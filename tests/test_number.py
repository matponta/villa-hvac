"""Tests for the house setpoint number (#2 setpoint push)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN

from .helpers import enable_supervisor, seed_thermostats

NUMBER = "number.house_setpoint"
SELECT = "select.house_mode"


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setpoint_pushes_temperature_to_all_zones(hass):
    await _setup(hass)  # mode defaults to Casa (offset 0)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort", temperature=20.0)
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": NUMBER, "value": 23.0}, blocking=True
    )
    await hass.async_block_till_done()

    targeted = {c.data["entity_id"]: c.data["temperature"] for c in temps}
    assert len(targeted) == 17
    assert all(t == 23.0 for t in targeted.values())  # Casa: base + 0


async def test_summer_via_offset(hass):
    await _setup(hass)  # salotto seeded 'cool' -> season auto=summer
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort", temperature=20.0)
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Via"}, blocking=True
    )
    await hass.async_block_till_done()

    # Summer Via = base(24) + 5 = 29 on every controllable zone.
    assert {c.data["temperature"] for c in temps} == {29.0}


async def test_winter_via_offset_when_thermostat_heating(hass):
    await _setup(hass)  # default setpoint 24
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort", temperature=20.0)
    # Reference thermostat in heat -> auto season = winter.
    hass.states.async_set(
        "climate.salotto_termostato_2", "heat",
        {"preset_mode": "comfort", "temperature": 20.0},
    )
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Via"}, blocking=True
    )
    await hass.async_block_till_done()

    # Winter Via = base(24) + (-2) = 22 (heating setback is cooler).
    assert {c.data["temperature"] for c in temps} == {22.0}


async def test_vacation_pushes_no_temperature(hass):
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort", temperature=20.0)
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": SELECT, "option": "Vacanza"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert temps == []  # building_protection: frost-fixed, no setpoint pushed
