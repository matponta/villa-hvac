"""v0.76.0 — winter safety: summer-only features must not act in heat mode."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import replace

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac import returnhome as rh
from custom_components.villa_hvac.const import (
    DOMAIN,
    HOUSE_MODE_AWAY,
    SEASON_REFERENCE_CLIMATE,
    SEASON_STAGIONE_SENSOR,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.villa_hvac.controller import calendar_season, current_season
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.policies import ThermalEstimator
from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot


# --- season fallback ----------------------------------------------------------

def test_calendar_season_boundaries():
    assert calendar_season(date(2026, 10, 14)) == SEASON_SUMMER
    assert calendar_season(date(2026, 10, 15)) == SEASON_WINTER
    assert calendar_season(date(2027, 1, 20)) == SEASON_WINTER
    assert calendar_season(date(2027, 4, 14)) == SEASON_WINTER
    assert calendar_season(date(2027, 4, 15)) == SEASON_SUMMER
    assert calendar_season(date(2027, 7, 1)) == SEASON_SUMMER


async def test_inconclusive_keeps_last_conclusive_winter(hass):
    """The old blind default was SUMMER: a winter KNX dropout would push the
    summer +5/+3 offsets onto heating thermostats. Now the last read holds."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat")
    assert current_season(hass, entry) == SEASON_WINTER
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "unavailable")
    hass.states.async_set(SEASON_STAGIONE_SENSOR, "unavailable")
    with freeze_time("2026-07-01 12:00:00"):  # even when the calendar says summer
        assert current_season(hass, entry) == SEASON_WINTER


async def test_inconclusive_without_memory_uses_calendar(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "unavailable")
    with freeze_time("2027-01-10 12:00:00"):
        assert current_season(hass, entry) == SEASON_WINTER
    other = MockConfigEntry(domain=DOMAIN, data={})
    with freeze_time("2027-07-10 12:00:00"):
        assert current_season(hass, other) == SEASON_SUMMER


# --- engine gates (free_air, #2b) ---------------------------------------------

async def _setup(hass, mode: str):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, mode, {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_free_air_is_inert_in_winter(hass):
    entry = await _setup(hass, "heat")
    hass.states.async_set("switch.free_air", "on")
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.season == SEASON_WINTER
    assert not any(z.paused for z in state.zones.values())
    # ...and it still pauses in summer (unchanged behaviour).
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "cool", {"preset_mode": "comfort"})
    summer = build_house_state(hass, entry, entry.runtime_data)
    assert any(z.paused for z in summer.zones.values() if z.emitter == "fancoil")


async def test_night_silence_not_active_in_winter(hass):
    entry = await _setup(hass, "heat")
    hass.states.async_set("select.house_mode", "Notte")
    hass.states.async_set("switch.auto_setback", "on")
    assert build_house_state(hass, entry, entry.runtime_data).night_active is False
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "cool", {"preset_mode": "comfort"})
    assert build_house_state(hass, entry, entry.runtime_data).night_active is True


# --- thermal estimator ----------------------------------------------------------

def _leader(temp):
    return ZoneSnapshot(
        zone_id="lr", name="LR", climate="climate.lr", emitter="fancoil",
        temp=temp, demand=False, enabled=True, fancoil="fan.x", s_eff=0.0,
    )


def test_estimator_does_not_learn_in_winter():
    est = ThermalEstimator()
    base = datetime(2027, 1, 10, 2, 0, 0)
    for m in range(0, 17, 2):
        est.observe(HouseState(
            now=base + timedelta(minutes=m),
            zones={"lr": _leader(21.0 + 0.4 * m / 60)},
            outdoor_temp=5.0, solar=0.0, consenso_freddo="off", blocco="off",
            model_learning_enabled=True, season=SEASON_WINTER,
        ))
    assert "lr" not in est.params


# --- #8 return-home -------------------------------------------------------------

def test_return_precond_inert_in_winter(monkeypatch):
    monkeypatch.setattr(rh, "return_precond_enabled", lambda h, e: True)
    monkeypatch.setattr(rh, "return_armed", lambda h, e: True)
    monkeypatch.setattr(rh, "return_date", lambda h, e: date(2027, 1, 15))
    monkeypatch.setattr(rh, "return_daypart", lambda h, e: "sera")
    z = ZoneSnapshot(zone_id="main_bedroom", name="P", climate="climate.mb",
                     emitter="fancoil", temp=17.0, enabled=True)
    src = HouseState(
        now=datetime(2027, 1, 15, 9, 0, tzinfo=datetime.now().astimezone().tzinfo),
        zones={"main_bedroom": z}, house_mode=HOUSE_MODE_AWAY, house_setpoint=21.0,
        mode_offset=-2.0, outdoor_temp=3.0, solar=100.0, season=SEASON_WINTER,
    )

    class _E:
        options: dict = {}

    ctrl = rh.AwayReturnController()
    assert ctrl.apply(src, None, _E(), commit=True) is src
    assert ctrl.decision is None
    # summer: the same armed Via is overridden (unchanged behaviour)
    summer = replace(src, season=SEASON_SUMMER)
    ctrl.apply(summer, None, _E(), commit=True)
    assert ctrl.decision is not None
