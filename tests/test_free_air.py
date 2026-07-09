"""Tests for #3 free-air / windows-open mode (manual house-wide cooling pause)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN, PRESET_BUILDING_PROTECTION
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.policies import house_mode_policy, window_pause_policy
from custom_components.villa_hvac.supervisor import preset_lever


async def _setup(hass):
    hass.states.async_set("climate.salotto_termostato_2", "cool",
                          {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_free_air_pauses_only_fancoil_zones(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    base = build_house_state(hass, entry, coordinator)
    assert not any(z.paused for z in base.zones.values())  # nothing paused

    hass.states.async_set("switch.free_air", "on")
    on = build_house_state(hass, entry, coordinator)
    fancoil = [z for z in on.zones.values() if z.emitter == "fancoil"]
    assert fancoil and all(z.paused for z in fancoil)             # cooled zones paused
    assert not any(z.paused for z in on.zones.values() if z.emitter != "fancoil")

    hass.states.async_set("switch.free_air", "off")
    off = build_house_state(hass, entry, coordinator)
    assert not any(z.paused for z in off.zones.values())          # released


async def test_free_air_forces_building_protection_and_skips_house_mode(hass):
    entry = await _setup(hass)
    hass.states.async_set("switch.free_air", "on")
    state = build_house_state(hass, entry, entry.runtime_data)

    bp = window_pause_policy(state)
    assert bp  # non-empty: the paused fancoil zones are forced to BP
    assert all(v == PRESET_BUILDING_PROTECTION for v in bp.values())

    # #2a must NOT push a comfort preset onto a free-air-paused zone.
    hm = house_mode_policy(state)
    for z in state.zones.values():
        if z.emitter == "fancoil" and z.climate:
            assert preset_lever(z.climate) not in hm


async def test_free_air_does_not_mutate_window_paused(hass):
    """The union must copy window.paused, never mutate the WindowController set."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    window = getattr(coordinator, "window", None)
    assert window is not None
    before = set(window.paused)

    hass.states.async_set("switch.free_air", "on")
    build_house_state(hass, entry, coordinator)

    assert window.paused == before  # unchanged
