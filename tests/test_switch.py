"""Tests for the per-zone enable switch (#10 long-term zone disable)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN, ZONES

from .helpers import enable_supervisor, seed_thermostats

CLIMATE = "climate.salotto_termostato_2"
SWITCH = "switch.salotto_enabled"


async def _setup(hass):
    """Set up the integration with the Salotto thermostat in 'comfort'."""
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_switch_created_only_for_zones_with_climate(hass):
    """A switch exists for each zone with a thermostat, not for the kitchen."""
    await _setup(hass)

    # Only fancoil zones with a thermostat get a switch (the verified lever).
    expected = sum(
        1
        for z in ZONES.values()
        if z.get("climate") and z.get("emitter") == "fancoil"
    )
    enabled = [
        s for s in hass.states.async_entity_ids("switch") if s.endswith("_enabled")
    ]
    assert len(enabled) == expected
    # The global #2 auto-setback switch exists alongside the per-zone ones.
    assert hass.states.get("switch.auto_setback") is not None
    # Kitchen follows the Salotto thermostat (climate is None) -> no switch.
    assert hass.states.get("switch.kitchen_enabled") is None
    # Radiant zones (e.g. lavanderia) are excluded even though they have a climate.
    assert hass.states.get("switch.lavanderia_enabled") is None
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get("switch.steady_pacing").state == "off"
    assert hass.states.get("switch.paced_living_room").state == "off"


async def test_turn_off_forces_building_protection(hass):
    """Disabling a zone -> the engine forces building_protection on it."""
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )

    # Only Salotto changes (others already in the Casa preset): forced to BP.
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == CLIMATE
    assert calls[0].data["preset_mode"] == "building_protection"
    assert hass.states.get(SWITCH).state == "off"


async def test_turn_on_restores_mode_preset(hass):
    """Re-enabling -> the engine restores the current house-mode preset."""
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    # Simulate the KNX thermostat actually applying building_protection.
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "building_protection"})
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )

    # Last write to Salotto restores the Casa preset (comfort).
    salotto = [c for c in calls if c.data["entity_id"] == CLIMATE]
    assert salotto[-1].data["preset_mode"] == "comfort"
    assert hass.states.get(SWITCH).state == "on"
