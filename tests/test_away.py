"""Tests for away auto-escalation (#2c)."""
from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from homeassistant.util import dt as dt_util

from custom_components.villa_hvac.away import escalation_target, restore_target
from custom_components.villa_hvac.const import DOMAIN, PRESENCE_GROUP

SELECT = "select.house_mode"


# --- Pure decision -----------------------------------------------------------

def test_escalation_target():
    assert escalation_target("Casa") == "Via"
    assert escalation_target("Notte") == "Via"
    assert escalation_target("Via") is None  # already away
    assert escalation_target("Vacanza") is None  # don't downgrade vacation


def test_restore_target():
    assert restore_target("Via") == "Casa"
    assert restore_target("Notte") is None  # never auto-leave Notte
    assert restore_target("Vacanza") is None
    assert restore_target("Casa") is None


# --- Integration -------------------------------------------------------------

async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_long_absence_escalates_to_via(hass):
    await _setup(hass)  # mode defaults to Casa
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")

    hass.states.async_set(PRESENCE_GROUP, "not_home")
    await hass.async_block_till_done()
    # Jump past the default 18 h escalation delay.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=18, minutes=1))
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Via"


async def test_brief_absence_does_not_escalate(hass):
    await _setup(hass)
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")

    hass.states.async_set(PRESENCE_GROUP, "not_home")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Casa"  # still home mode


async def test_return_home_restores_casa_from_via(hass):
    await _setup(hass)
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Via"}, blocking=True
    )
    await hass.async_block_till_done()

    hass.states.async_set(PRESENCE_GROUP, "home")
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Casa"
