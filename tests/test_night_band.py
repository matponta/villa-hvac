"""v0.96.0 — night band + wake pre-heat (STORY_NIGHT_SCHEDULE.md)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache,
)
from homeassistant.core import State

from custom_components.villa_hvac.const import DOMAIN, SEASON_REFERENCE_CLIMATE
from custom_components.villa_hvac.supervisor import temperature_lever
from custom_components.villa_hvac.supervisor.night_band import (
    BAND_ACTIVE,
    BAND_IDLE,
    BAND_OFF,
    BAND_PREHEAT,
    NightBandLatch,
    band_window,
    night_band_plan,
    room_lead,
)

from .helpers import enable_supervisor, seed_thermostats

ROME = ZoneInfo("Europe/Rome")
START, END = time(21, 30), time(7, 0)
H3 = timedelta(hours=3)


def _rome(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ROME).astimezone(timezone.utc)


def _plan(now, *, wake=None, leads=None, latch=None, active_mode=True, enabled=True):
    return night_band_plan(
        now, enabled=enabled, active_mode=active_mode, start=START, end=END,
        tz=ROME, wake=wake, leads=leads or {"a": H3, "b": timedelta(hours=1)},
        latch=latch or NightBandLatch(),
    )


# --- Pure: the window ----------------------------------------------------------

def test_window_wraps_midnight():
    assert band_window(_rome(2026, 11, 24, 21, 29), START, END, ROME) is None
    w = band_window(_rome(2026, 11, 24, 21, 30), START, END, ROME)
    assert w == (_rome(2026, 11, 24, 21, 30), _rome(2026, 11, 25, 7, 0))
    assert band_window(_rome(2026, 11, 25, 3, 0), START, END, ROME) == w
    assert band_window(_rome(2026, 11, 25, 6, 59), START, END, ROME) == w
    assert band_window(_rome(2026, 11, 25, 7, 0), START, END, ROME) is None
    assert band_window(_rome(2026, 11, 25, 12, 0), START, END, ROME) is None
    assert band_window(_rome(2026, 11, 25, 3, 0), START, START, ROME) is None


def test_window_across_the_dst_change():
    """25/10/2026 CEST->CET at 03:00: the night is 10.5 h of real time and the
    band still ends at 07:00 on the wall clock."""
    w = band_window(_rome(2026, 10, 25, 2, 30), START, END, ROME)
    assert w is not None
    start, end = w
    assert end - start == timedelta(hours=10, minutes=30)
    assert end == datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)   # 07:00 CET
    assert band_window(end - timedelta(minutes=1), START, END, ROME) == w
    assert band_window(end, START, END, ROME) is None


def test_off_and_not_casa_trim_nothing():
    plan, _ = _plan(_rome(2026, 11, 24, 23, 0), enabled=False)
    assert plan.status == BAND_OFF and not plan.trimmed
    plan, _ = _plan(_rome(2026, 11, 24, 23, 0), active_mode=False)
    assert plan.status == BAND_IDLE and not plan.trimmed
    assert plan.reason == "modalita_non_casa"


def test_without_alarm_every_room_ends_at_the_fixed_end():
    plan, _ = _plan(_rome(2026, 11, 25, 6, 59))
    assert plan.status == BAND_ACTIVE and plan.trimmed == {"a", "b"}
    plan, _ = _plan(_rome(2026, 11, 25, 7, 0))
    assert plan.status == BAND_IDLE and not plan.trimmed


# --- Pure: the alarm and the per-room pre-heat ---------------------------------

def test_alarm_ends_each_room_its_lead_before_the_wake():
    wake = time(6, 30)
    plan, latch = _plan(_rome(2026, 11, 25, 3, 29), wake=wake)
    assert plan.wake == _rome(2026, 11, 25, 6, 30)
    assert plan.room_end == {"a": _rome(2026, 11, 25, 3, 30),
                             "b": _rome(2026, 11, 25, 5, 30)}
    assert plan.trimmed == {"a", "b"} and plan.status == BAND_ACTIVE
    plan, latch = _plan(_rome(2026, 11, 25, 3, 30), wake=wake, latch=latch)
    assert plan.trimmed == {"b"} and plan.status == BAND_PREHEAT
    plan, latch = _plan(_rome(2026, 11, 25, 5, 30), wake=wake, latch=latch)
    assert not plan.trimmed and plan.status == BAND_PREHEAT
    plan, _ = _plan(_rome(2026, 11, 25, 6, 30), wake=wake, latch=latch)
    assert plan.status == BAND_IDLE


def test_alarm_never_stretches_the_band_past_the_end():
    plan, _ = _plan(_rome(2026, 11, 25, 7, 30), wake=time(9, 30),
                    leads={"a": timedelta(minutes=30)})
    assert not plan.trimmed      # outside the 21:30-07:00 window
    plan, _ = _plan(_rome(2026, 11, 25, 6, 30), wake=time(9, 30),
                    leads={"a": timedelta(minutes=30)})
    assert plan.room_end["a"] == _rome(2026, 11, 25, 7, 0)


def test_alarm_set_after_the_preheat_should_have_started_exits_at_once():
    """Set at 04:30 for 06:30 with a 3 h lead: room a leaves at once, and a
    shorter lead later (warmer forecast) does not put it back in the band."""
    plan, latch = _plan(_rome(2026, 11, 25, 4, 0))                      # no alarm yet
    assert plan.trimmed == {"a", "b"}
    plan, latch = _plan(_rome(2026, 11, 25, 4, 30), wake=time(6, 30), latch=latch)
    assert "a" not in plan.trimmed and "b" in plan.trimmed
    plan, latch = _plan(_rome(2026, 11, 25, 4, 35), wake=time(6, 30), latch=latch,
                        leads={"a": timedelta(minutes=30), "b": timedelta(hours=1)})
    assert "a" not in plan.trimmed                                      # no jump back


def test_alarm_switched_off_mid_night_returns_to_the_fixed_end():
    plan, latch = _plan(_rome(2026, 11, 25, 4, 0), wake=time(6, 30))
    assert "a" not in plan.trimmed                       # pre-heating since 03:30
    plan, latch = _plan(_rome(2026, 11, 25, 4, 5), latch=latch)      # alarm off
    assert plan.trimmed == {"a", "b"}
    assert plan.room_end["a"] == _rome(2026, 11, 25, 7, 0)


def test_alarm_far_from_the_night_is_ignored():
    """An alarm at 21:00 is not this night's wake (it would stretch to the
    next evening): fixed end."""
    plan, _ = _plan(_rome(2026, 11, 24, 23, 0), wake=time(21, 0))
    assert plan.wake is None and plan.trimmed == {"a", "b"}


def test_restart_mid_window_rebuilds_the_same_plan():
    now = _rome(2026, 11, 25, 2, 0)
    first, _ = _plan(now, wake=time(6, 30))
    fresh, _ = _plan(now, wake=time(6, 30), latch=NightBandLatch())
    assert first == fresh


# --- Pure: the lead ------------------------------------------------------------

def test_lead_is_clamped_and_falls_back_to_the_cap():
    assert room_lead(90.0, 0.84, H3) == timedelta(minutes=90)
    assert room_lead(300.0, 0.84, H3) == H3          # W3 lesson: never explode
    assert room_lead(-5.0, 0.84, H3) == timedelta(0)
    assert room_lead(90.0, 0.3, H3) == H3            # model unsure
    assert room_lead(None, 0.9, H3) == H3            # unreachable room
    assert room_lead(None, None, H3) == H3


def test_unreachable_room_uses_the_cap():
    from custom_components.villa_hvac.supervisor.winter_model import (
        WinterParams,
        recovery_minutes,
    )

    weak = WinterParams(a=0.2, k_h=0.4, n_a=50, n_k=50)   # T_inf = 5 + 2 = 7 °C
    rec = recovery_minutes(20.0, 21.0, 5.0, weak)
    assert rec is None
    assert room_lead(rec, weak.confidence(), H3) == H3


# --- With HA -----------------------------------------------------------------

PADRONALE = "climate.camera_padronale_termostato_2"
SWITCH = "switch.fascia_notturna"
SENSOR = "sensor.fascia_notturna"


async def _setup(hass, *, season="heat"):
    await hass.config.async_set_time_zone("Europe/Rome")
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, season)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _written(calls, entity):
    return [c.data["temperature"] for c in calls if c.data["entity_id"] == entity]


async def test_winter_band_trims_casa_by_the_night_offset(hass, freezer):
    freezer.move_to(_rome(2026, 11, 24, 22, 0))
    entry = await _setup(hass)
    seed_thermostats(hass, hvac="heat", temperature=21.0)
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_preset_mode")
    await enable_supervisor(hass)
    engine = entry.runtime_data.engine

    await engine._run()
    assert 20.0 not in _written(temps, PADRONALE)        # switch still off

    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH},
                                   blocking=True)
    await hass.async_block_till_done()
    assert _written(temps, PADRONALE)[-1] == 20.0        # 21 - 1
    st = hass.states.get(SENSOR)
    assert st.state == "attiva"
    assert st.attributes["offset"] == -1.0
    assert st.attributes["start"] == "21:30" and st.attributes["end"] == "07:00"
    assert st.attributes["alarm"] is False


async def test_via_and_vacanza_own_their_setpoints(hass, freezer):
    freezer.move_to(_rome(2026, 11, 24, 23, 0))
    entry = await _setup(hass)
    seed_thermostats(hass, hvac="heat", temperature=21.0)
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_preset_mode")
    hass.states.async_set(SWITCH, "on")
    await enable_supervisor(hass)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.house_mode", "option": "Via"},
        blocking=True)
    await hass.async_block_till_done()
    await entry.runtime_data.engine._run()
    assert _written(temps, PADRONALE)[-1] == 19.0         # Via -2, no night trim
    assert hass.states.get(SENSOR).attributes["reason"] == "modalita_non_casa"


async def test_alarm_shows_per_room_lead_on_the_sensor(hass, freezer):
    freezer.move_to(_rome(2026, 11, 24, 23, 0))
    entry = await _setup(hass)
    hass.states.async_set("input_boolean.wakeup_active", "on")
    hass.states.async_set("input_datetime.wake_up_time", "06:30:00")
    hass.states.async_set(SWITCH, "on")
    await entry.runtime_data.engine._run()
    attrs = hass.states.get(SENSOR).attributes
    assert attrs["alarm"] is True and attrs["alarm_time"] == "06:30"
    room = attrs["rooms"]["main_bedroom"]
    assert 0 <= room["lead_min"] <= 180
    assert room["end"].startswith("2026-11-25T0")


async def test_band_edge_burst_does_not_concede_the_setpoint(hass, freezer):
    """The band start changes every room's target at once, like a mode change.
    A burst of passes before the KNX echo must write once and wait (v0.94.0),
    never conclude a manual override."""
    freezer.move_to(_rome(2026, 11, 24, 21, 30))
    entry = await _setup(hass)
    seed_thermostats(hass, hvac="heat", temperature=21.0)   # the echo never lands
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_preset_mode")
    await enable_supervisor(hass)
    engine = entry.runtime_data.engine
    await engine._run()
    hass.states.async_set(SWITCH, "on")
    for _ in range(6):
        await engine._run()
    assert _written(temps, PADRONALE).count(20.0) == 1
    lever = temperature_lever(PADRONALE)
    assert engine.lever_decisions[lever]["note"] == "pending"
    assert engine._lever_states[lever].override_until is None


async def test_sleep_helper_unavailable_is_not_sleeping(hass, freezer):
    from custom_components.villa_hvac.engine import build_house_state

    freezer.move_to(_rome(2026, 7, 4, 3, 0))
    entry = await _setup(hass, season="cool")
    hass.states.async_set("switch.auto_setback", "on")
    hass.states.async_set("input_boolean.notte", "unavailable")
    assert build_house_state(hass, entry, entry.runtime_data).night_active is False
    hass.states.async_set("input_boolean.notte", "on")
    assert build_house_state(hass, entry, entry.runtime_data).night_active is True


async def test_restored_notte_comes_back_as_casa(hass):
    mock_restore_cache(hass, [State("select.house_mode", "Notte")])
    await _setup(hass)
    assert hass.states.get("select.house_mode").state == "Casa"


@pytest.mark.parametrize("name", ["inizio", "fine", "inizio_estate", "fine_estate"])
async def test_time_entities_exist(hass, name):
    await _setup(hass)
    assert hass.states.get(f"time.fascia_notturna_{name}") is not None
    assert hass.states.get(SWITCH).state == "off"
