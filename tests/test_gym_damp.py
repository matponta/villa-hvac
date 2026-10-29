"""Tests for the v0.90.0 palestra + bagno palestra damp control
(STORY_GYM_DAMP.md): the pure law, the split's `dry`, the dehumidifier
controller, the hand-back, the sensors, alerts and knobs."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    DOMAIN,
    GYM_DEHUM_POWER,
    GYM_DEHUM_SWITCH,
    GYM_RH_BAGNO,
    GYM_RH_FALLBACK,
    GYM_RH_PALESTRA,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
    ZONES,
)
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.policies import GymDampController, SplitGroupController
from custom_components.villa_hvac.supervisor import (
    HouseState,
    LeverState,
    ZoneSnapshot,
    hvac_mode_lever,
    power_lever,
    split_head_target,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor.gym_damp import (
    REASON_BLIND,
    REASON_DEHUM,
    REASON_DISABLED,
    REASON_DRY,
    REASON_FALLBACK,
    REASON_SPLIT_AND_DEHUM,
    REASON_SPLIT_BUSY,
    REASON_SPLIT_DRY,
    SOURCE_FALLBACK,
    SOURCE_KNX,
    SOURCE_KNX_BAGNO,
    SOURCE_KNX_PALESTRA,
    SOURCE_NONE,
    dew_point,
    gym_damp_decision,
    gym_rh_source,
)

T0 = dt.datetime(2026, 10, 29, 22, 0, tzinfo=dt.UTC)
M = dt.timedelta(minutes=1)
PALESTRA = "climate.aircon_palestra_2"
GARAGE = "climate.aircon_garage_2"
PLUG = power_lever(GYM_DEHUM_SWITCH)


# --- pure core -------------------------------------------------------------------

def test_source_prefers_the_knx_pair_then_the_up_sense():
    assert gym_rh_source(63.6, 62.3, 67.0) == (63.6, SOURCE_KNX)
    assert gym_rh_source(None, 62.3, 67.0) == (62.3, SOURCE_KNX_BAGNO)
    assert gym_rh_source(63.6, None, 67.0) == (63.6, SOURCE_KNX_PALESTRA)
    assert gym_rh_source(None, None, 67.0) == (67.0, SOURCE_FALLBACK)
    assert gym_rh_source(None, None, None) == (None, SOURCE_NONE)


def test_the_ep1_is_not_even_an_input():
    # the EP1 3febdc reads 15-25 points low: it must never be wired in
    assert "3febdc" not in ZONES["palestra"]["humidity_sensor"]
    assert ZONES["palestra"]["humidity_sensor"] == GYM_RH_PALESTRA
    assert ZONES["bagno_palestra"]["humidity_sensor"] == GYM_RH_BAGNO


def test_dew_point_matches_the_brief():
    assert dew_point(21.7, 63.6) == pytest.approx(14.6, abs=0.2)
    assert dew_point(None, 60.0) is None
    assert dew_point(20.0, 0.0) is None


def _d(rh_p, rh_b=None, rh_f=None, **kw):
    args = dict(
        summer=False, active=False, split_available=True, split_yielding=False,
        stage2_elapsed=None, stage2_delay=120 * M, blind_for=None,
        blind_hold=dt.timedelta(hours=2), ceiling=62.0, release=55.0,
    )
    args.update(kw)
    return gym_damp_decision(rh_p, rh_b, rh_f, **args)


def test_winter_hysteresis_dehumidifier_only():
    assert _d(61.0).reason == REASON_DRY and not _d(61.0).dehum
    d = _d(62.0)
    assert d.active and d.dehum and d.split_humidity is None and d.reason == REASON_DEHUM
    assert _d(58.0, active=True).dehum                  # inside the band: keep going
    assert not _d(55.0, active=True).dehum              # release reached
    assert not _d(58.0, active=False).dehum             # below ceiling, not started


def test_the_worst_knx_room_decides():
    assert _d(58.0, 63.0).dehum                         # bagno above the ceiling


def test_summer_split_first_then_stage2():
    d = _d(64.0, summer=True)
    assert (d.dehum, d.split_humidity, d.reason) == (False, "dry", REASON_SPLIT_DRY)
    d = _d(64.0, summer=True, stage2_elapsed=119 * M)
    assert not d.dehum
    d = _d(64.0, summer=True, stage2_elapsed=120 * M)
    assert (d.dehum, d.split_humidity, d.reason) == (True, "dry", REASON_SPLIT_AND_DEHUM)


def test_summer_split_busy_or_missing_falls_to_the_dehumidifier():
    d = _d(64.0, summer=True, split_yielding=True)
    assert (d.dehum, d.split_humidity, d.reason) == (True, None, REASON_SPLIT_BUSY)
    d = _d(64.0, summer=True, split_available=False)
    assert (d.dehum, d.split_humidity, d.reason) == (True, None, REASON_DEHUM)


def test_fallback_and_blind():
    d = _d(None, None, 66.0)
    assert d.dehum and d.reason == REASON_FALLBACK and d.source == SOURCE_FALLBACK
    held = _d(None, active=True, blind_for=dt.timedelta(minutes=90))
    assert held.dehum and held.reason == REASON_BLIND
    assert not _d(None, active=False, blind_for=M).dehum
    gone = _d(None, active=True, blind_for=dt.timedelta(hours=2))
    assert not gone.dehum and not gone.active and gone.reason == REASON_BLIND


# --- the split head's `dry` -------------------------------------------------------

def _tgt(humidity_mode, *, summer=True, home=True, occupied=False, temp=23.0):
    return split_head_target(
        "comfort", summer=summer, home=home, occupied=occupied, temp=temp, rh=None,
        cantina_setpoint=19.0, comfort_setpoint=24.0, rh_ceiling=70.0, rh_floor=55.0,
        humidity_mode=humidity_mode,
    )


def test_split_never_dries_in_winter():
    assert _tgt("dry", summer=False, occupied=True, temp=26.0) == ("off", None)


def test_split_dries_an_empty_or_away_gym_in_summer():
    assert _tgt("dry") == ("dry", None)
    assert _tgt("dry", home=False, occupied=True, temp=27.0) == ("dry", None)


def test_split_cools_an_occupied_warm_gym_cooling_dries_too():
    assert _tgt("dry", occupied=True, temp=25.0) == ("cool", 24.0)
    assert _tgt("dry", occupied=True, temp=23.0) == ("dry", None)


def test_split_without_a_damp_ask_is_unchanged():
    assert _tgt(None) == ("off", None)
    assert _tgt(None, occupied=True) == ("cool", 24.0)


# --- controller -------------------------------------------------------------------

def _palestra(mode="off", occupied=False):
    return ZoneSnapshot(
        zone_id="palestra", name="Palestra", climate="climate.palestra_termostato_2",
        emitter="radiant", ac_group="split_trio", split_climate=PALESTRA,
        split_role="comfort", split_mode=mode, split_temp=22.0, occupied=occupied,
    )


def _house(*, now=T0, enabled=True, split=True, summer=False, rh_p=63.6, rh_b=62.3,
           rh_f=67.0, plug=False, mode=HOUSE_MODE_HOME, garage=None, occupied=False):
    zones = {"palestra": _palestra(occupied=occupied)}
    if garage is not None:
        zones["garage"] = ZoneSnapshot(
            zone_id="garage", name="Garage", climate=GARAGE, emitter="split_ac",
            ac_group="split_trio", split_climate=GARAGE, split_role="manual",
            split_mode=garage,
        )
    return HouseState(
        now=now, zones=zones, season=SEASON_SUMMER if summer else SEASON_WINTER,
        house_mode=mode, house_setpoint=25.0, split_enabled=split,
        split_palestra_setpoint=24.0, split_min_on=5 * M, split_min_off=3 * M,
        gym_enabled=enabled, gym_rh_palestra=rh_p, gym_rh_bagno=rh_b,
        gym_rh_fallback=rh_f, gym_temp=21.7, gym_dehum_on=plug, gym_dehum_power=0.0,
    )


def test_winter_drives_only_the_plug():
    gym = GymDampController()
    out = gym(_house())
    assert out == {PLUG: "on"}
    assert gym.decision.split_humidity is None
    assert gym.view["reason"] == REASON_DEHUM
    assert gym.view["dewpoint_palestra"] == pytest.approx(14.6, abs=0.2)


def test_plug_min_on_and_min_off():
    gym = GymDampController()
    assert gym(_house())[PLUG] == "on"
    assert gym(_house(now=T0 + 10 * M, rh_p=50.0, rh_b=50.0))[PLUG] == "on"   # min-on 30
    assert gym(_house(now=T0 + 30 * M, rh_p=50.0, rh_b=50.0))[PLUG] == "off"
    assert gym(_house(now=T0 + 35 * M))[PLUG] == "off"                       # min-off 15
    assert gym(_house(now=T0 + 45 * M))[PLUG] == "on"


def test_disabled_emits_nothing_but_keeps_the_view():
    gym = GymDampController()
    assert gym(_house(enabled=False)) == {}
    assert gym.decision is None
    assert gym.view["reason"] == REASON_DISABLED
    assert gym.view["wanted_reason"] == REASON_DEHUM
    gym2 = GymDampController()
    gym2.observe_dark(_house())
    assert gym2.view["rh_ctrl"] == 63.6 and gym2.view["enabled"] is False


def test_snapshot_is_what_we_found():
    gym = GymDampController()
    assert gym.failsafe_power() == {}
    gym(_house(plug=False))
    gym(_house(now=T0 + M, plug=True))                  # our own write landed
    assert gym.failsafe_power() == {GYM_DEHUM_SWITCH: "off"}
    gym.handed_back()
    assert gym.failsafe_power() == {}
    assert GymDampController().failsafe_power() == {}   # never took it


def _pair():
    gym = GymDampController()
    return gym, SplitGroupController(gym)


def _run(gym, split, state):
    return {**gym(state), **split(state)}


def test_summer_split_dries_the_empty_gym_whoever_is_home():
    gym, split = _pair()
    out = _run(gym, split, _house(summer=True, mode=HOUSE_MODE_AWAY))
    assert out[hvac_mode_lever(PALESTRA)] == "dry"
    assert temperature_lever(PALESTRA) not in out
    assert out[PLUG] == "off"


def test_summer_stage2_adds_the_dehumidifier():
    gym, split = _pair()
    _run(gym, split, _house(summer=True))
    out = _run(gym, split, _house(now=T0 + 119 * M, summer=True))
    assert out[PLUG] == "off"
    out = _run(gym, split, _house(now=T0 + 120 * M, summer=True))
    assert out[PLUG] == "on" and out[hvac_mode_lever(PALESTRA)] == "dry"
    assert gym.view["reason"] == REASON_SPLIT_AND_DEHUM


def test_summer_without_the_split_switch_uses_the_dehumidifier():
    gym, split = _pair()
    out = _run(gym, split, _house(summer=True, split=False))
    assert out == {PLUG: "on"}


def test_summer_garage_heat_turns_the_split_off_and_the_plug_on():
    gym, split = _pair()
    out = _run(gym, split, _house(summer=True, garage="heat"))
    assert out[hvac_mode_lever(PALESTRA)] == "off"
    assert out[PLUG] == "on"
    assert gym.view["reason"] == REASON_SPLIT_BUSY


def test_gym_disabled_leaves_the_split_as_before():
    gym, split = _pair()
    out = _run(gym, split, _house(summer=True, enabled=False))
    assert out[hvac_mode_lever(PALESTRA)] == "off"     # empty gym: comfort says off
    assert PLUG not in out


# --- integration --------------------------------------------------------------------

async def _setup(hass):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_house_state_reads_the_knx_pair_with_freshness(hass):
    entry = await _setup(hass)
    hass.states.async_set(GYM_RH_PALESTRA, "63.6")
    hass.states.async_set(GYM_RH_BAGNO, "unavailable")
    hass.states.async_set(GYM_RH_FALLBACK, "67")
    hass.states.async_set(GYM_DEHUM_SWITCH, "off")
    hass.states.async_set(GYM_DEHUM_POWER, "1.2")
    s = build_house_state(hass, entry, entry.runtime_data)
    assert (s.gym_rh_palestra, s.gym_rh_bagno, s.gym_rh_fallback) == (63.6, None, 67.0)
    assert s.gym_dehum_on is False and s.gym_dehum_power == 1.2
    assert (s.gym_rh_ceiling, s.gym_rh_release) == (62.0, 55.0)
    assert s.gym_stage2_delay == 120 * M and not s.gym_enabled
    assert s.zones["palestra"].humidity == 63.6
    with freeze_time(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=31)):
        s = build_house_state(hass, entry, entry.runtime_data)
        assert s.gym_rh_palestra is None                # silent > 30 min -> stale


async def test_fail_safe_restores_the_plug_as_found(hass):
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    gym = entry.runtime_data.gym
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    await engine.async_fail_safe()                      # never took it
    assert GYM_DEHUM_SWITCH not in [c.data.get("entity_id") for c in on + off]
    gym(replace(_house(plug=False), gym_enabled=True))
    engine._lever_states[PLUG] = LeverState(written="on")
    await engine.async_fail_safe()
    assert {"entity_id": GYM_DEHUM_SWITCH} in [c.data for c in off]
    assert PLUG not in engine._lever_states
    assert gym.failsafe_power() == {}


async def test_switch_off_hands_the_plug_back(hass):
    entry = await _setup(hass)
    on = async_mock_service(hass, "switch", "turn_on")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.gym_damp"}, blocking=True,
    )
    gym = entry.runtime_data.gym
    gym(_house(plug=True))
    on.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.gym_damp"}, blocking=True,
    )
    assert {"entity_id": GYM_DEHUM_SWITCH} in [c.data for c in on]


async def test_sensors(hass):
    entry = await _setup(hass)
    entry.runtime_data.gym.observe_dark(_house())
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    st = hass.states.get("sensor.hvac_palestra_umidita")
    assert st.state == REASON_DISABLED
    assert st.attributes["wanted_reason"] == REASON_DEHUM
    assert float(hass.states.get("sensor.palestra_umidita_di_controllo").state) == 63.6
    assert float(hass.states.get("sensor.palestra_punto_di_rugiada").state) == pytest.approx(
        14.6, abs=0.2
    )


async def test_numbers_drive_the_settings(hass):
    from custom_components.villa_hvac.controller import gym_settings
    entry = await _setup(hass)
    assert gym_settings(hass, entry)["gym_rh_ceiling"] == 62.0
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.palestra_umidita_massima", "value": 58},
        blocking=True,
    )
    assert gym_settings(hass, entry)["gym_rh_ceiling"] == 58.0


async def test_idle_dehumidifier_alert_once(hass):
    entry = await _setup(hass)
    sent = async_mock_service(hass, "notify", "mobile_app_matphone16")
    async_mock_service(hass, "notify", "mobile_app_pixel_10")
    from custom_components.villa_hvac.damp import GymDampAlerts
    gym = entry.runtime_data.gym
    gym(replace(_house(plug=True), gym_dehum_power=2.0))
    alerts = GymDampAlerts(hass, entry)
    await alerts.async_check(T0)
    await alerts.async_check(T0 + 29 * M)
    assert not sent
    await alerts.async_check(T0 + 30 * M)
    await alerts.async_check(T0 + 60 * M)
    assert [c.data["title"] for c in sent] == ["Deumidificatore palestra fermo"]
