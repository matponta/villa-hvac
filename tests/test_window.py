"""Tests for window pause (#4)."""
from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from homeassistant.util import dt as dt_util

from custom_components.villa_hvac.const import DOMAIN, WINDOW_OPEN_DELAY
from custom_components.villa_hvac.window import window_zones

from .helpers import enable_supervisor, seed_thermostats

WINDOW = "cover.vasistas_gabriele"  # this vasistas is in the Gabriele BATHROOM
CLIMATE = "climate.bagno_gabriele_termostato_2"


def test_window_zones_maps_the_three_vasistas():
    mapping = dict(window_zones())
    assert mapping == {
        "bagno_gabriele": "cover.vasistas_gabriele",
        "bagno_giochi": "cover.vasistas_bagno_sala_giochi",
        "lavanderia": "cover.vasistas_lavanderia",
    }


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_open_window_pauses_after_debounce(hass):
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    # Nothing yet — still inside the debounce window.
    assert calls == []

    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    # Window pause (priority) -> BP on the windowed zone only. (A coordinator
    # tick may re-assert it since the mock never applies the state — that's the
    # dropped-telegram path — so assert what's written, not an exact count.)
    assert calls
    assert all(c.data["entity_id"] == CLIMATE for c in calls)
    assert all(c.data["preset_mode"] == "building_protection" for c in calls)


async def test_brief_open_then_close_does_not_pause(hass):
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    hass.states.async_set(WINDOW, "closed")  # closed before debounce elapses
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert calls == []  # timer was cancelled -> never paused -> no write


async def test_close_restores_current_mode_preset(hass):
    await _setup(hass)  # mode defaults to Casa -> comfort
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()
    calls.clear()
    # Simulate the KNX thermostat actually applying building_protection.
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "building_protection"})

    hass.states.async_set(WINDOW, "closed")
    await hass.async_block_till_done()

    assert calls
    assert all(c.data["entity_id"] == CLIMATE for c in calls)
    assert all(c.data["preset_mode"] == "comfort" for c in calls)  # Casa preset
