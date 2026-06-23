"""Tests for the house-mode → preset driver (#2a)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.controller import (
    controllable_zones,
    preset_for_mode,
)

SELECT = "select.house_mode"
AUTO = "switch.auto_setback"


# --- Pure mapping ------------------------------------------------------------

def test_preset_for_mode():
    assert preset_for_mode("Casa") == "comfort"
    assert preset_for_mode("Via") == "standby"
    assert preset_for_mode("Notte") == "economy"
    assert preset_for_mode("Vacanza") == "building_protection"
    assert preset_for_mode("nonsense") is None


def test_controllable_zones_are_the_17_thermostats():
    zones = dict(controllable_zones())
    # All 8 fancoil thermostats + radiant zones; never split-AC or no-climate.
    assert "living_room" in zones
    assert "lavanderia" in zones  # radiant, still preset-controllable
    assert "kitchen" not in zones  # no own climate
    assert "rack" not in zones  # no climate
    assert "cantina_vini" not in zones  # split AC
    assert "garage" not in zones  # split AC
    assert len(zones) == 17
    assert all(eid.endswith("_termostato_2") for eid in zones.values())


# --- Integration through the select / switch ---------------------------------

async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _select_mode(hass, mode):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": SELECT, "option": mode}, blocking=True,
    )


async def test_selecting_night_applies_economy_to_all_controllable(hass):
    await _setup(hass)
    calls = async_mock_service(hass, "climate", "set_preset_mode")
    # Notte also triggers the camere-silenziose overlay (#2b) on the bedrooms.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "fan", "turn_off")

    await _select_mode(hass, "Notte")

    targeted = {c.data["entity_id"]: c.data["preset_mode"] for c in calls}
    assert len(targeted) == 17
    assert all(p == "economy" for p in targeted.values())
    # Split-AC zones are never touched.
    assert "climate.aircon_cantina_vini_2" not in targeted
    assert "climate.aircon_garage_2" not in targeted


async def test_auto_setback_off_writes_nothing(hass):
    await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": AUTO}, blocking=True
    )
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    await _select_mode(hass, "Vacanza")

    assert calls == []


async def test_disabled_zone_is_skipped(hass):
    await _setup(hass)
    calls = async_mock_service(hass, "climate", "set_preset_mode")
    # #10-disable one zone (this itself sets building_protection on it).
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.sala_giochi_enabled"}, blocking=True,
    )
    calls.clear()  # ignore the building_protection write from the disable

    await _select_mode(hass, "Casa")

    targeted = {c.data["entity_id"] for c in calls}
    assert "climate.sala_giochi_termostato_2" not in targeted
    assert "climate.salotto_termostato_2" in targeted  # others still driven
