"""Tests for the fused per-zone temperature (#1)."""
from __future__ import annotations

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.temperature import TempSource, fuse_temperature

MAX_AGE = 1800.0  # 30 min, matches TEMP_STALE_AFTER


# --- Pure fusion logic -------------------------------------------------------

def test_fresh_primary_wins():
    value, source = fuse_temperature(
        [TempSource("sensor", 22.0, 10), TempSource("climate", 24.0, 10)], MAX_AGE
    )
    assert (value, source) == (22.0, "sensor")


def test_missing_primary_falls_back():
    value, source = fuse_temperature(
        [TempSource("sensor", None, None), TempSource("climate", 24.0, 10)], MAX_AGE
    )
    assert (value, source) == (24.0, "climate")


def test_stale_primary_falls_back_to_fresh():
    value, source = fuse_temperature(
        [TempSource("sensor", 22.0, 4000), TempSource("climate", 24.0, 10)], MAX_AGE
    )
    assert (value, source) == (24.0, "climate")


def test_all_stale_returns_none():
    value, source = fuse_temperature(
        [TempSource("sensor", 22.0, 4000), TempSource("climate", 24.0, 4000)], MAX_AGE
    )
    assert (value, source) == (None, None)


def test_no_sources_returns_none():
    assert fuse_temperature([], MAX_AGE) == (None, None)


# --- Integration through the coordinator/sensor ------------------------------

async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_zone_uses_clima_twin_as_primary(hass):
    """Salotto fused temp reads the clima_* twin, not the climate attribute."""
    hass.states.async_set("sensor.clima_salotto", "23.4")
    hass.states.async_set(
        "climate.salotto_termostato_2", "cool", {"current_temperature": 25.0}
    )
    await _setup(hass)

    state = hass.states.get("sensor.salotto_temperature")
    assert state is not None
    assert float(state.state) == 23.4
    assert state.attributes["source"] == "sensor"


async def test_zone_falls_back_to_climate_when_primary_unavailable(hass):
    """When clima_* is unavailable, fused temp uses climate current_temperature."""
    hass.states.async_set("sensor.clima_salotto", "unavailable")
    hass.states.async_set(
        "climate.salotto_termostato_2", "cool", {"current_temperature": 25.0}
    )
    await _setup(hass)

    state = hass.states.get("sensor.salotto_temperature")
    assert float(state.state) == 25.0
    assert state.attributes["source"] == "climate"


async def test_radiant_zone_has_temperature_sensor(hass):
    """Radiant zones (no fancoil, no EP) still expose a fused temperature."""
    hass.states.async_set("sensor.clima_lavanderia", "21.7")
    await _setup(hass)

    state = hass.states.get("sensor.lavanderia_temperature")
    assert state is not None
    assert float(state.state) == 21.7


@freeze_time("2026-07-15 10:00:00")
async def test_unchanged_cyclic_report_uses_last_reported(hass):
    """A flat KNX value remains fresh when its cyclic telegram is reported."""
    hass.states.async_set("sensor.clima_salotto", "23.4")
    original = hass.states.get("sensor.clima_salotto")
    assert original is not None
    original_updated = original.last_updated
    original_reported = original.last_reported

    with freeze_time("2026-07-15 11:00:00"):
        hass.states.async_set("sensor.clima_salotto", "23.4")
        reported = hass.states.get("sensor.clima_salotto")
        assert reported is not None
        assert reported.last_updated == original_updated
        assert reported.last_reported > original_reported
        entry = await _setup(hass)
        assert entry.runtime_data.data["zone_temps"]["living_room"]["value"] == 23.4
