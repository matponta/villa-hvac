"""v0.78.0 — winter solar gain: open the sunny covers while away, put back at sunset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN, SEASON_REFERENCE_CLIMATE
from custom_components.villa_hvac.supervisor.model import CoverInfo
from custom_components.villa_hvac.supervisor.sungain import (
    SunCover,
    WinterSunState,
    winter_sun_step,
)
from custom_components.villa_hvac.sungain import WinterSunController, _decode, _encode

T0 = datetime(2027, 1, 10, 11, 0, tzinfo=timezone.utc)
DAY = "2027-01-10"


def _step(covers, state=None, *, now=T0, day=DAY, active=True, away=True,
          elevation=20.0, sun_on=frozenset({"south"}), bright=True):
    return winter_sun_step(
        active=active, away=away, sun_elevation=elevation, sun_on=sun_on,
        bright=bright, covers=covers, now=now, local_day=day,
        state=state or WinterSunState(), min_elevation=5.0, tolerance=4.0,
        grace=timedelta(minutes=5),
    )


S = SunCover("cover.s", "south", position=0)
W = SunCover("cover.w", "west", position=0)


def test_opens_only_the_sunny_facade_once_per_day():
    st, cmd = _step([S, W])
    assert cmd == {"cover.s": 100}
    assert st.opened["cover.s"][0] == 0
    # next tick (cover now open): nothing re-sent
    st2, cmd2 = _step([SunCover("cover.s", "south", position=100), W], st,
                      now=T0 + timedelta(minutes=1))
    assert cmd2 == {}
    assert "cover.s" in st2.opened


def test_no_open_when_dim_low_sun_home_or_inactive():
    assert _step([S], bright=False)[1] == {}
    assert _step([S], elevation=3.0)[1] == {}
    assert _step([S], away=False)[1] == {}
    assert _step([S], active=False)[1] == {}


def test_skips_blocked_moving_unknown_and_already_open():
    covers = [
        SunCover("cover.b", "south", blocked=True, position=0),
        SunCover("cover.m", "south", position=30, moving=True),
        SunCover("cover.u", "south", position=None),
        SunCover("cover.o", "south", position=100),
    ]
    st, cmd = _step(covers)
    assert cmd == {}
    assert st.opened == {}
    assert "cover.o" in st.done            # already open: handled, not "ours"
    assert "cover.m" not in st.done        # retried once it settles


def test_sunset_puts_back_only_what_we_opened_to_its_prior_position():
    st, _ = _step([SunCover("cover.s", "south", position=24)])
    st, cmd = _step([SunCover("cover.s", "south", position=100),
                     SunCover("cover.o", "south", position=100)], st,
                    now=T0 + timedelta(hours=5), elevation=-1.0)
    assert cmd == {"cover.s": 24}
    assert st.opened == {}


def test_owner_takeover_is_respected_not_closed_at_sunset():
    st, _ = _step([S])
    # owner lowered it after the grace period
    st, cmd = _step([SunCover("cover.s", "south", position=40)], st,
                    now=T0 + timedelta(minutes=10))
    assert cmd == {} and st.opened == {}
    # and it is not re-opened the same day
    st, cmd = _step([SunCover("cover.s", "south", position=40)], st,
                    now=T0 + timedelta(minutes=11))
    assert cmd == {}
    assert _step([SunCover("cover.s", "south", position=40)], st,
                 now=T0 + timedelta(hours=6), elevation=-2.0)[1] == {}


def test_travelling_cover_within_grace_is_not_a_takeover():
    st, _ = _step([S])
    st, _ = _step([SunCover("cover.s", "south", position=50, moving=True)], st,
                  now=T0 + timedelta(minutes=10))
    assert "cover.s" in st.opened
    st, _ = _step([SunCover("cover.s", "south", position=50)], st,
                  now=T0 + timedelta(minutes=2))
    assert "cover.s" in st.opened


def test_coming_home_forgets_without_writing():
    st, _ = _step([S])
    st, cmd = _step([SunCover("cover.s", "south", position=100)], st,
                    now=T0 + timedelta(hours=1), away=False)
    assert cmd == {} and st.opened == {}
    assert "cover.s" in st.done  # still no re-open today if they leave again


def test_new_day_rearms():
    st, _ = _step([S])
    st, cmd = _step([S], st, now=T0 + timedelta(days=1), day="2027-01-11")
    assert cmd == {"cover.s": 100}


def test_unknown_elevation_holds():
    st, _ = _step([S])
    st2, cmd = _step([SunCover("cover.s", "south", position=100)], st, elevation=None)
    assert cmd == {} and st2.opened == st.opened


def test_store_roundtrip():
    st, _ = _step([S])
    back = _decode(_encode(st))
    assert back.day == st.day and back.done == st.done
    assert back.opened["cover.s"][0] == 0
    assert _decode(None) == WinterSunState()
    assert _decode({"opened": {"x": "garbage"}}).opened == {}


# --- HA wiring ----------------------------------------------------------------------

async def test_controller_opens_in_winter_via_and_skips_blocked(hass, monkeypatch):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    sup = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_supervisor")
    hass.states.async_set(sup, "on")
    hass.states.async_set("select.house_mode", "Via")
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": 20.0, "azimuth": 180.0})
    hass.states.async_set("sensor.gw3000a_solar_radiation", "400")
    hass.states.async_set("cover.a", "closed", {"current_position": 0})
    hass.states.async_set("cover.b", "closed", {"current_position": 0})

    calls = []

    async def _svc(call):
        calls.append(dict(call.data))

    hass.services.async_register("cover", "set_cover_position", _svc)
    engine = entry.runtime_data.engine
    engine._covers_cache = (
        CoverInfo("cover.a", "south", zone="studio_v"),
        CoverInfo("cover.b", "south", zone="office"),
    )
    ctrl: WinterSunController = entry.runtime_data.winter_sun
    import custom_components.villa_hvac.sungain as mod

    monkeypatch.setattr(mod, "shade_blocked", lambda h, e, zone: zone == "office")
    await ctrl._evaluate()
    assert calls == [{"entity_id": "cover.a", "position": 100}]

    # summer: never
    calls.clear()
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "cool", {"preset_mode": "comfort"})
    monkeypatch.setattr(mod, "shade_blocked", lambda h, e, zone: False)
    ctrl.state = WinterSunState()  # fresh day: only the season gate stands
    await ctrl._evaluate()
    assert calls == []
    # ...and the same fresh state in winter would open both (gate is the season)
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    await ctrl._evaluate()
    assert {c["entity_id"] for c in calls} == {"cover.a", "cover.b"}
