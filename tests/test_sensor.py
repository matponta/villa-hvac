"""Tests for the #11 HVAC plan sensor.

The pure projection is covered in test_supervisor.py (build_plan); here we check
the HA-facing sensor renders the engine's plan view — and crucially that the plan
is computed and exposed even while the supervisor is deploy-dark (master off).
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN

CLIMATE = "climate.salotto_termostato_2"


async def _setup(hass):
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _plan_entity_id(hass, entry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_hvac_plan"
    )


async def test_plan_sensor_renders_while_deploy_dark(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    engine = coordinator.engine
    assert engine.enabled is False  # master off (deploy-dark)

    # No actuation should ever happen while dark.
    preset_calls = async_mock_service(hass, "climate", "set_preset_mode")

    await engine._tick()
    assert engine.plan_view is not None
    assert len(preset_calls) == 0  # dark: plan computed, nothing actuated

    coordinator.async_update_listeners()  # push the fresh plan to the entity
    await hass.async_block_till_done()

    eid = _plan_entity_id(hass, entry)
    assert eid is not None
    state = hass.states.get(eid)
    assert state is not None
    assert state.state == engine.plan_view.summary
    assert state.attributes["supervisor_on"] is False
    assert state.attributes["season"] == "summer"
    assert "forecast" in state.attributes
    assert isinstance(state.attributes["zones"], list)
    # every configured zone is represented
    assert {z["zone"] for z in state.attributes["zones"]} == {
        z.zone_id for z in engine.plan_view.zones
    }


async def test_plan_sensor_surfaces_precool_window(hass):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    engine = coordinator.engine
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "25.0")  # cool now
    hass.states.async_set("switch.duty_cycle", "on")  # pre-cool is gated by #9
    # Inject a hot peak ahead + a fresh stamp so the live fetch is skipped.
    engine._forecast = [(dt_util.utcnow() + timedelta(hours=4), 34.0)]
    engine._forecast_ts = dt_util.utcnow()

    await engine._tick()
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(_plan_entity_id(hass, entry))
    assert state.state == "pre_cool"
    assert state.attributes["precool"] is True
    assert state.attributes["forecast_peak"] == 34.0
    assert state.attributes["peak_eta_minutes"] == 240.0
