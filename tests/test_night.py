"""Tests for camere silenziose: night silence + heat-guard (#2b)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    NIGHT_GUARD_HIGH,
    NIGHT_GUARD_LOW,
    DOMAIN,
)
from custom_components.villa_hvac.night import GuardState, bedrooms, evaluate_guard

T0 = datetime(2026, 6, 23, 2, 0, 0, tzinfo=timezone.utc)
THRESHOLD = 26.0


# --- Pure hysteresis ---------------------------------------------------------

def test_bedrooms_are_exactly_two():
    ids = {zid for zid, _ in bedrooms()}
    assert ids == {"main_bedroom", "gabriroom"}  # studio_v (Ospiti) is now office


def test_silent_stays_silent_below_threshold():
    state, action = evaluate_guard(GuardState(), 24.0, THRESHOLD, T0)
    assert action is None and not state.cooling


def test_brief_warmth_does_not_trigger_cooling():
    state = GuardState()
    state, action = evaluate_guard(state, 27.0, THRESHOLD, T0)
    assert action is None  # timer just started
    state, action = evaluate_guard(
        state, 27.0, THRESHOLD, T0 + NIGHT_GUARD_HIGH - timedelta(seconds=1)
    )
    assert action is None and not state.cooling


def test_sustained_warmth_triggers_low_cooling():
    state = GuardState(above_since=T0)
    state, action = evaluate_guard(state, 27.0, THRESHOLD, T0 + NIGHT_GUARD_HIGH)
    assert action == "cool" and state.cooling


def test_sustained_cool_silences_again():
    state = GuardState(cooling=True, below_since=T0)
    state, action = evaluate_guard(state, 24.0, THRESHOLD, T0 + NIGHT_GUARD_LOW)
    assert action == "silence" and not state.cooling


def test_cooling_holds_until_low_window_elapses():
    state = GuardState(cooling=True)
    state, action = evaluate_guard(state, 24.0, THRESHOLD, T0)
    assert action is None and state.cooling  # below timer just started


def test_missing_temp_is_noop():
    state = GuardState(cooling=True, below_since=T0)
    new, action = evaluate_guard(state, None, THRESHOLD, T0 + NIGHT_GUARD_LOW)
    assert action is None and new == state


# --- Integration: mode transitions drive the bedrooms ------------------------

async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _select_mode(hass, mode):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.house_mode", "option": mode}, blocking=True,
    )
    await hass.async_block_till_done()


async def test_night_silences_bedrooms_and_casa_wakes(hass):
    await _setup(hass)
    async_mock_service(hass, "climate", "set_preset_mode")
    sw_on = async_mock_service(hass, "switch", "turn_on")
    sw_off = async_mock_service(hass, "switch", "turn_off")
    fan_off = async_mock_service(hass, "fan", "turn_off")

    await _select_mode(hass, "Notte")

    on_targets = {c.data["entity_id"] for c in sw_on}
    fan_targets = {c.data["entity_id"] for c in fan_off}
    assert "switch.fancoil_camera_padronale_manuale" in on_targets
    assert "switch.fancoil_camera_gabriele_manuale" in on_targets
    assert "fan.fancoil_camera_padronale" in fan_targets
    assert "fan.fancoil_camera_gabriele" in fan_targets

    await _select_mode(hass, "Casa")

    off_targets = {c.data["entity_id"] for c in sw_off}
    assert "switch.fancoil_camera_padronale_manuale" in off_targets
    assert "switch.fancoil_camera_gabriele_manuale" in off_targets
