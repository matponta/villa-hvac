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
          elevation=20.0, sun_on=frozenset({"south"}), bright=True, raining=False):
    return winter_sun_step(
        active=active, away=away, sun_elevation=elevation, sun_on=sun_on,
        bright=bright, covers=covers, now=now, local_day=day,
        state=state or WinterSunState(), min_elevation=5.0, tolerance=4.0,
        grace=timedelta(minutes=5), raining=raining,
        dim_hold=timedelta(minutes=30), max_opens=3,
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


def test_home_stop_keeps_ours_and_sunset_still_puts_them_back():
    """Review v0.78.0: home 13:00, out 14:00 with #2c still on Casa -> the covers
    must not stay open all night."""
    st, _ = _step([S])
    up = SunCover("cover.s", "south", position=100)
    st, cmd = _step([up], st, now=T0 + timedelta(hours=1), away=False)
    assert cmd == {} and "cover.s" in st.opened        # kept, nothing written
    st, cmd = _step([up], st, now=T0 + timedelta(hours=6), away=False, elevation=-1.0)
    assert cmd == {"cover.s": 0}
    # ...but no NEW open while home
    assert _step([SunCover("cover.w", "south", position=0)], away=False)[1] == {}


def test_inactive_forgets_without_writing():
    st, _ = _step([S])
    st, cmd = _step([SunCover("cover.s", "south", position=100)], st, active=False)
    assert cmd == {} and st.opened == {}
    assert st.opens == {"cover.s": 1}   # today's open still counts


def test_shade_block_after_open_is_hands_off():
    st, _ = _step([S])
    st, cmd = _step([SunCover("cover.s", "south", blocked=True, position=100)], st,
                    now=T0 + timedelta(hours=6), elevation=-1.0)
    assert cmd == {} and st.opened == {}


def test_slightly_short_of_open_is_not_a_takeover():
    st, _ = _step([S])
    st, _ = _step([SunCover("cover.s", "south", position=92)], st,
                  now=T0 + timedelta(minutes=10))
    assert "cover.s" in st.opened


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


# --- v0.83.0: lost sun put-back, rain, up to 3 opens a day ------------------------

UP = SunCover("cover.s", "south", position=100)
DOWN = SunCover("cover.s", "south", position=0)


def test_dim_for_30_min_puts_back_what_we_opened():
    st, _ = _step([S])
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=1), bright=False)
    assert cmd == {} and st.dim_since == T0 + timedelta(minutes=1)
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=30), bright=False)
    assert cmd == {}                                   # 29 min: still waiting
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=31), bright=False)
    assert cmd == {"cover.s": 0} and st.opened == {}


def test_a_bright_minute_resets_the_dim_timer():
    st, _ = _step([S])
    st, _ = _step([UP], st, now=T0 + timedelta(minutes=1), bright=False)
    st, _ = _step([UP], st, now=T0 + timedelta(minutes=20), bright=True)
    assert st.dim_since is None
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=40), bright=False)
    assert cmd == {}
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=71), bright=False)
    assert cmd == {"cover.s": 0}


def test_rain_puts_back_at_once_and_blocks_opening():
    st, _ = _step([S])
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=2), raining=True)
    assert cmd == {"cover.s": 0} and st.opened == {}
    # bright but raining: no open
    st, cmd = _step([DOWN], st, now=T0 + timedelta(minutes=5), raining=True)
    assert cmd == {}
    assert _step([S], raining=True)[1] == {}


def test_put_back_runs_in_any_mode_and_never_touches_owner_covers():
    st, _ = _step([S])
    owner = SunCover("cover.o", "south", position=100)     # was already open
    st, _ = _step([UP, owner], st, now=T0 + timedelta(minutes=1))
    st, cmd = _step([UP, owner], st, now=T0 + timedelta(minutes=3),
                    away=False, raining=True)
    assert cmd == {"cover.s": 0}


def test_sun_comes_back_reopens_up_to_three_times_a_day():
    st, t = WinterSunState(), T0
    for n in range(1, 4):
        st, cmd = _step([DOWN], st, now=t)
        assert cmd == {"cover.s": 100} and st.opens["cover.s"] == n
        st, _ = _step([UP], st, now=t + timedelta(minutes=1), bright=False)
        st, cmd = _step([UP], st, now=t + timedelta(minutes=32), bright=False)
        assert cmd == {"cover.s": 0}
        t += timedelta(minutes=40)
    st, cmd = _step([DOWN], st, now=t)
    assert cmd == {}                                    # 4th: cap reached
    # ...and the next day it re-arms
    st, cmd = _step([DOWN], st, now=t + timedelta(days=1), day="2027-01-11")
    assert cmd == {"cover.s": 100} and st.opens == {"cover.s": 1}


def test_owner_takeover_still_blocks_reopening_for_the_day():
    st, _ = _step([S])
    st, _ = _step([SunCover("cover.s", "south", position=20)], st,
                  now=T0 + timedelta(minutes=10))
    assert "cover.s" in st.done and st.opened == {}
    st, cmd = _step([SunCover("cover.s", "south", position=20)], st,
                    now=T0 + timedelta(hours=1))
    assert cmd == {}


def test_replay_2026_10_20_short_spell_then_rain():
    """11:10 one bright tick opened 8 covers that stayed up in the rain until
    sunset. Now: put back 30 min after the spell (rain at 12:09 anyway)."""
    st, cmd = _step([DOWN], now=T0)
    assert cmd == {"cover.s": 100}
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=4), bright=False)
    assert cmd == {}
    st, cmd = _step([UP], st, now=T0 + timedelta(minutes=34), bright=False)
    assert cmd == {"cover.s": 0}
    st, cmd = _step([DOWN], st, now=T0 + timedelta(minutes=60), bright=False, raining=True)
    assert cmd == {}


def test_store_roundtrip_v083_and_legacy_migration():
    st, _ = _step([S])
    st, _ = _step([UP], st, now=T0 + timedelta(minutes=1), bright=False)
    back = _decode(_encode(st))
    assert back == st
    # pre-v0.83.0 store: our opened cover sat in `done` too
    legacy = {
        "day": DAY, "done": ["cover.s", "cover.o"],
        "opened": {"cover.s": [0, T0.isoformat()]},
    }
    mig = _decode(legacy)
    assert mig.done == frozenset({"cover.o"}) and mig.opens == {"cover.s": 1}
    assert mig.opened["cover.s"][0] == 0 and mig.dim_since is None


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


async def test_failed_sunset_put_back_is_retried_then_given_up(hass, monkeypatch):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    hass.states.async_set(
        reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_supervisor"), "on"
    )
    hass.states.async_set("select.house_mode", "Via")
    hass.states.async_set("sun.sun", "below_horizon", {"elevation": -3.0, "azimuth": 250.0})
    hass.states.async_set("cover.a", "open", {"current_position": 100})
    entry.runtime_data.engine._covers_cache = (CoverInfo("cover.a", "south", zone="x"),)
    ctrl: WinterSunController = entry.runtime_data.winter_sun
    ctrl.state = WinterSunState(day=None, opened={"cover.a": (0, T0)})
    import custom_components.villa_hvac.sungain as mod

    writes = []

    async def _fail(self, eid, pos):
        writes.append((eid, pos))
        return False

    monkeypatch.setattr(mod.WinterSunController, "_write", _fail)
    await ctrl._evaluate()
    assert writes == [("cover.a", 0)] and "cover.a" in ctrl.state.opened  # retried
    for _ in range(20):
        await ctrl._evaluate()
    assert len(writes) == mod.WINTER_SUN_CLOSE_RETRIES   # bounded
    assert ctrl.state.opened == {}


async def test_holds_when_season_is_only_a_guess(hass):
    """No live season signal -> the cycle holds (doesn't forget/move)."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    hass.states.async_set(
        reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_supervisor"), "on"
    )
    hass.states.async_set("select.house_mode", "Casa")
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "unavailable")
    ctrl: WinterSunController = entry.runtime_data.winter_sun
    held = WinterSunState(day="2027-01-10", opened={"cover.a": (0, T0)})
    ctrl.state = held
    await ctrl._evaluate()
    assert ctrl.state is held


async def test_controller_puts_back_when_it_rains(hass):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    hass.states.async_set(
        reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_supervisor"), "on"
    )
    hass.states.async_set("select.house_mode", "Via")
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": 20.0, "azimuth": 180.0})
    hass.states.async_set("sensor.gw3000a_solar_radiation", "400")
    hass.states.async_set("sensor.gw3000a_rain_rate", "7.8")
    hass.states.async_set("cover.a", "open", {"current_position": 100})
    hass.states.async_set("cover.b", "closed", {"current_position": 0})
    calls = []

    async def _svc(call):
        calls.append(dict(call.data))

    hass.services.async_register("cover", "set_cover_position", _svc)
    entry.runtime_data.engine._covers_cache = (
        CoverInfo("cover.a", "south", zone="x"),
        CoverInfo("cover.b", "south", zone="y"),
    )
    ctrl: WinterSunController = entry.runtime_data.winter_sun
    import custom_components.villa_hvac.sungain as mod

    day = mod.dt_util.as_local(mod.dt_util.utcnow()).date().isoformat()
    ctrl.state = WinterSunState(day=day, opened={"cover.a": (0, T0)}, opens={"cover.a": 1})
    await ctrl._evaluate()
    assert calls == [{"entity_id": "cover.a", "position": 0}]   # back down, b not opened
    assert ctrl.state.opened == {}
    # dry again + bright: both may open (a 2nd time, b 1st)
    calls.clear()
    hass.states.async_set("sensor.gw3000a_rain_rate", "0.0")
    hass.states.async_set("cover.a", "closed", {"current_position": 0})
    await ctrl._evaluate()
    assert {c["entity_id"] for c in calls} == {"cover.a", "cover.b"}
    assert ctrl.state.opens == {"cover.a": 2, "cover.b": 1}
