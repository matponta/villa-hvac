"""Tests for the v0.87.0 wine cellar (STORY_CELLAR.md): season windows, the
fan-mixed reading, the head's humidity hysteresis, the circulation fan, the
alerts and the sensors."""
from __future__ import annotations

import datetime as dt

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    CELLAR_FAN,
    DOMAIN,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.villa_hvac.engine import SupervisorEngine
from custom_components.villa_hvac.policies import SplitGroupController
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    fan_mode_lever,
    hvac_mode_lever,
    power_lever,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor.cellar import (
    CELLAR_COLD,
    CELLAR_DAMP,
    CELLAR_DRY,
    CELLAR_OK,
    CELLAR_UNKNOWN,
    CELLAR_WARM,
    REASON_DRYING,
    REASON_HOLD,
    REASON_NO_SAMPLE,
    REASON_RELAXED,
    CellarSample,
    cellar_fan_wanted,
    cellar_head,
    cellar_sample,
    cellar_status,
    cellar_window,
    sample_fresh,
)

CANTINA = "climate.aircon_cantina_vini_2"
CANTINA_RH = "sensor.everything_presence_one_a8c934_humidity"
CANTINA_T = "sensor.ep_cantina_vini_temperature"
T0 = dt.datetime(2026, 10, 28, 12, 5, tzinfo=dt.UTC)
MIX = dt.timedelta(minutes=8)


def _w(summer=True):
    return cellar_window(summer, summer_setpoint=18.0, winter_setpoint=18.0,
                         rh_floor=55.0, rh_ceiling=70.0)


def _s(temp=19.0, rh=60.0):
    return CellarSample(temp, rh, T0)


# --- pure ---------------------------------------------------------------------

def test_season_windows():
    s, w = _w(True), _w(False)
    assert (s.setpoint, s.t_low, s.t_high) == (18.0, 16.0, 20.0)
    assert (w.setpoint, w.t_low, w.t_high) == (18.0, 12.0, 19.0)
    assert (s.rh_floor, s.rh_ceiling) == (w.rh_floor, w.rh_ceiling) == (55.0, 70.0)


def test_sample_only_on_mixed_air():
    since = T0 - dt.timedelta(minutes=5)
    # fan on for 5 min only: the stagnant pocket is still around the sensor
    assert cellar_sample(None, fan_on=True, fan_since=since, now=T0,
                         temp=23.5, rh=42.0, mix_min=MIX) is None
    s = cellar_sample(None, fan_on=True, fan_since=since, now=T0 + MIX,
                      temp=19.5, rh=50.0, mix_min=MIX)
    assert (s.temp, s.rh) == (19.5, 50.0)
    # fan off again: keep the mixed reading, never the stagnant one
    later = T0 + dt.timedelta(minutes=40)
    assert cellar_sample(s, fan_on=False, fan_since=later, now=later,
                         temp=23.5, rh=42.0, mix_min=MIX) is s
    assert sample_fresh(s, T0 + MIX + dt.timedelta(minutes=90),
                        dt.timedelta(minutes=90)) is s
    assert sample_fresh(s, T0 + MIX + dt.timedelta(minutes=91),
                        dt.timedelta(minutes=90)) is None


def test_head_holds_the_setpoint_in_band():
    d = cellar_head(_w(), _s(rh=62.0), drying=False, relaxed=False, hysteresis=3.0)
    assert (d.mode, d.setpoint, d.reason) == ("cool", 18.0, REASON_HOLD)


def test_head_dries_with_hysteresis():
    w = _w()
    d = cellar_head(w, _s(temp=18.2, rh=72.0), drying=False, relaxed=False,
                    hysteresis=3.0)
    assert (d.mode, d.setpoint, d.reason, d.drying) == ("dry", None, REASON_DRYING, True)
    # warm and damp: cooling dries too
    d = cellar_head(w, _s(temp=20.0, rh=72.0), drying=False, relaxed=False,
                    hysteresis=3.0)
    assert (d.mode, d.setpoint) == ("cool", 18.0)
    # 68 % keeps drying (ceiling - 3 = 67), 66 % stops
    assert cellar_head(w, _s(temp=18.2, rh=68.0), drying=True, relaxed=False,
                       hysteresis=3.0).drying
    assert not cellar_head(w, _s(temp=18.2, rh=66.0), drying=True, relaxed=False,
                           hysteresis=3.0).drying
    # 68 % does not START drying
    assert not cellar_head(w, _s(rh=68.0), drying=False, relaxed=False,
                           hysteresis=3.0).drying


def test_head_relaxes_when_dry_capped_at_t_high():
    d = cellar_head(_w(True), _s(rh=50.0), drying=False, relaxed=False, hysteresis=3.0)
    assert (d.setpoint, d.reason, d.relaxed) == (19.5, REASON_RELAXED, True)
    d = cellar_head(_w(False), _s(rh=50.0), drying=False, relaxed=False, hysteresis=3.0)
    assert d.setpoint == 19.0          # winter: relax 1.0, t_high 19
    # stays relaxed up to floor + 3, released above
    assert cellar_head(_w(), _s(rh=57.0), drying=False, relaxed=True,
                       hysteresis=3.0).relaxed
    assert not cellar_head(_w(), _s(rh=58.5), drying=False, relaxed=True,
                           hysteresis=3.0).relaxed


def test_head_without_sample_is_plain_cool():
    d = cellar_head(_w(), None, drying=True, relaxed=True, hysteresis=3.0)
    assert (d.mode, d.setpoint, d.reason) == ("cool", 18.0, REASON_NO_SAMPLE)
    assert not d.drying and not d.relaxed


@pytest.mark.parametrize("temp,rh,status", [
    (19.0, 62.0, CELLAR_OK), (20.5, 62.0, CELLAR_WARM), (15.0, 62.0, CELLAR_COLD),
    (19.0, 72.0, CELLAR_DAMP), (19.0, 50.0, CELLAR_DRY),
])
def test_status(temp, rh, status):
    assert cellar_status(_w(True), _s(temp, rh)) == status


def test_status_unknown_and_winter_cold_edge():
    assert cellar_status(_w(), None) == CELLAR_UNKNOWN
    assert cellar_status(_w(False), _s(13.0, 62.0)) == CELLAR_OK   # winter: fine
    assert cellar_status(_w(False), _s(11.5, 62.0)) == CELLAR_COLD


def test_fan_duty():
    assert cellar_fan_wanted(T0.replace(minute=0), run_minutes=20, drying=False)
    assert cellar_fan_wanted(T0.replace(minute=19), run_minutes=20, drying=False)
    assert not cellar_fan_wanted(T0.replace(minute=20), run_minutes=20, drying=False)
    assert cellar_fan_wanted(T0.replace(minute=45), run_minutes=20, drying=True)


# --- controller -----------------------------------------------------------------

def _house(*, now=T0, enabled=True, summer=True, fan_on=True,
           fan_for=dt.timedelta(minutes=10), temp=19.5, rh=60.0):
    z = ZoneSnapshot(
        zone_id="cantina_vini", name="Cantina", climate=CANTINA, emitter="split_ac",
        ac_group="split_trio", split_climate=CANTINA, split_role="storage",
        split_temp=19.0, humidity=rh,
    )
    return HouseState(
        now=now, zones={"cantina_vini": z}, split_enabled=enabled,
        season=SEASON_SUMMER if summer else SEASON_WINTER,
        split_cantina_setpoint=18.0, split_rh_ceiling=70.0, split_rh_floor=55.0,
        cellar_fan_on=fan_on, cellar_fan_since=now - fan_for, cellar_temp=temp,
    )


def test_controller_drives_head_and_fan():
    out = SplitGroupController()(_house())
    assert out[hvac_mode_lever(CANTINA)] == "cool"
    assert out[temperature_lever(CANTINA)] == 18.0
    assert out[fan_mode_lever(CANTINA)] == "low"
    assert out[power_lever(CELLAR_FAN)] == "on"           # minute 5 < 20
    out = SplitGroupController()(_house(now=T0.replace(minute=30)))
    assert out[power_lever(CELLAR_FAN)] == "off"


def test_controller_ignores_the_stagnant_reading():
    ctrl = SplitGroupController()
    # fan off: EP says 23.5 °C / 42 % -> would look dry; it must not count
    out = ctrl(_house(fan_on=False, temp=23.5, rh=42.0))
    assert out[temperature_lever(CANTINA)] == 18.0
    assert ctrl.cellar_view["reason"] == REASON_NO_SAMPLE
    assert ctrl.cellar_view["status"] == CELLAR_UNKNOWN
    assert ctrl.cellar_view["raw_humidity"] == 42.0


def test_controller_keeps_fan_on_while_drying():
    ctrl = SplitGroupController()
    out = ctrl(_house(now=T0.replace(minute=15), temp=18.2, rh=74.0))
    assert out[hvac_mode_lever(CANTINA)] == "dry"
    later = T0.replace(minute=40)
    out = ctrl(_house(now=later, fan_for=dt.timedelta(minutes=25), temp=18.2, rh=69.0))
    assert out[hvac_mode_lever(CANTINA)] == "dry"          # 69 > 67: still drying
    assert out[power_lever(CELLAR_FAN)] == "on"


def test_controller_disabled_emits_nothing_but_observes():
    ctrl = SplitGroupController()
    assert ctrl(_house(enabled=False, rh=50.0)) == {}
    assert ctrl.cellar_view["status"] == CELLAR_DRY
    assert ctrl.cellar_view["enabled"] is False
    ctrl2 = SplitGroupController()
    ctrl2.observe_dark(_house(rh=62.0))
    assert ctrl2.cellar_view["status"] == CELLAR_OK
    assert ctrl2.cellar_sample.rh == 62.0


# --- integration -------------------------------------------------------------------

async def _setup(hass):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_power_lever_turns_the_plug_fan_on_without_percentage(hass):
    entry = await _setup(hass)
    on = async_mock_service(hass, "fan", "turn_on")
    off = async_mock_service(hass, "fan", "turn_off")
    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[], controllers=[])
    await engine._dispatch_write(power_lever(CELLAR_FAN), "on")
    await engine._dispatch_write(power_lever(CELLAR_FAN), "off")
    assert [c.data for c in on] == [{"entity_id": CELLAR_FAN}]
    assert [c.data for c in off] == [{"entity_id": CELLAR_FAN}]


def _seed_view(entry, *, temp=21.5, rh=62.0, enabled=True):
    split = entry.runtime_data.split
    split.observe_dark(_house(enabled=enabled, temp=temp, rh=rh))
    split.cellar_view["enabled"] = enabled


async def test_warm_alert_once_after_three_hours(hass):
    with freeze_time("2026-10-28 12:00:00") as frozen:
        entry = await _setup(hass)
        sent = async_mock_service(hass, "notify", "mobile_app_matphone16")
        async_mock_service(hass, "notify", "mobile_app_pixel_10")
        from custom_components.villa_hvac.cellar import CellarAlerts
        alerts = CellarAlerts(hass, entry)
        _seed_view(entry, temp=21.5)                    # above t_high (20)
        now = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.UTC)
        await alerts.async_check(now)
        assert not sent
        frozen.tick(dt.timedelta(hours=3))
        await alerts.async_check(now + dt.timedelta(hours=3))
        await alerts.async_check(now + dt.timedelta(hours=4))
        assert [c.data["title"] for c in sent] == ["Cantina vini calda"]


async def test_alerts_switch_off_sends_nothing(hass):
    entry = await _setup(hass)
    sent = async_mock_service(hass, "notify", "mobile_app_matphone16")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.cellar_alerts"}, blocking=True,
    )
    from custom_components.villa_hvac.cellar import CellarAlerts
    alerts = CellarAlerts(hass, entry)
    _seed_view(entry, temp=25.0)
    now = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.UTC)
    await alerts.async_check(now)
    await alerts.async_check(now + dt.timedelta(hours=10))
    assert not sent


async def test_cellar_sensors(hass):
    entry = await _setup(hass)
    _seed_view(entry, temp=19.4, rh=58.0)
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cantina_vini_stato").state == CELLAR_OK
    assert float(hass.states.get("sensor.cantina_vini_temperatura").state) == 19.4
    assert float(hass.states.get("sensor.cantina_vini_umidita").state) == 58.0


# --- v0.88.0: garage heat yield + editable knobs -------------------------------

GARAGE = "climate.aircon_garage_2"


def _with_garage(state, mode):
    from dataclasses import replace
    g = ZoneSnapshot(
        zone_id="garage", name="Garage", climate=GARAGE, emitter="split_ac",
        ac_group="split_trio", split_climate=GARAGE, split_role="manual",
        split_mode=mode,
    )
    return replace(state, zones={**state.zones, "garage": g},
                   split_min_on=dt.timedelta(minutes=5),
                   split_min_off=dt.timedelta(minutes=3))


def test_wine_head_yields_to_garage_heat_after_min_on():
    ctrl = SplitGroupController()
    out = ctrl(_with_garage(_house(summer=False), "off"))
    assert out[hvac_mode_lever(CANTINA)] == "cool"
    # garage heat 2 min later: min-on (5) not served -> keep cooling
    out = ctrl(_with_garage(_house(now=T0 + dt.timedelta(minutes=2), summer=False), "heat"))
    assert out[hvac_mode_lever(CANTINA)] == "cool"
    out = ctrl(_with_garage(_house(now=T0 + dt.timedelta(minutes=6), summer=False), "heat"))
    assert out[hvac_mode_lever(CANTINA)] == "off"
    assert temperature_lever(CANTINA) not in out
    assert power_lever(CELLAR_FAN) in out                 # circulation goes on
    assert ctrl.cellar_view["reason"] == "cede_al_garage"
    assert hvac_mode_lever(GARAGE) not in out             # garage never commanded
    # garage done: back to cool once min-off (3) is served
    out = ctrl(_with_garage(_house(now=T0 + dt.timedelta(minutes=8), summer=False), "off"))
    assert out[hvac_mode_lever(CANTINA)] == "off"
    out = ctrl(_with_garage(_house(now=T0 + dt.timedelta(minutes=10), summer=False), "off"))
    assert out[hvac_mode_lever(CANTINA)] == "cool"


def test_garage_auto_counts_as_heat_only_in_winter():
    s = _with_garage(_house(summer=False), "auto")
    assert SplitGroupController._heat_call(s, summer=False)
    assert not SplitGroupController._heat_call(s, summer=True)
    assert not SplitGroupController._heat_call(_with_garage(_house(), "cool"), True)


async def test_cellar_numbers_drive_the_settings(hass):
    from custom_components.villa_hvac.controller import cellar_settings
    entry = await _setup(hass)
    s = cellar_settings(hass, entry)
    assert s["cellar_setpoint_summer"] == 19.0 and s["cellar_setpoint_winter"] == 19.0
    assert (s["cellar_rh_floor"], s["cellar_rh_ceiling"]) == (55.0, 70.0)
    assert s["cellar_fan_minutes"] == 20.0
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.cantina_temperatura_inverno", "value": 20.5},
        blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.cantina_ventilatore_minuti_per_ora", "value": 30},
        blocking=True,
    )
    s = cellar_settings(hass, entry)
    assert s["cellar_setpoint_winter"] == 20.5
    assert s["cellar_fan_minutes"] == 30.0
