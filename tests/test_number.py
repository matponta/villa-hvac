"""Tests for the house setpoint number (#2 setpoint push)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN

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
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": NUMBER, "value": 23.0}, blocking=True
    )
    await hass.async_block_till_done()

    targeted = {c.data["entity_id"]: c.data["temperature"] for c in temps}
    assert len(targeted) == 17
    assert all(t == 23.0 for t in targeted.values())  # Casa: base + 0


async def test_mode_offset_is_added_to_setpoint(hass):
    await _setup(hass)  # default setpoint 24
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Via"}, blocking=True
    )
    await hass.async_block_till_done()

    # Via = standby = base(24) + 2 = 26 on every controllable zone.
    assert {c.data["temperature"] for c in temps} == {26.0}


async def test_vacation_pushes_no_temperature(hass):
    await _setup(hass)
    async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": SELECT, "option": "Vacanza"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert temps == []  # building_protection: frost-fixed, no setpoint pushed
