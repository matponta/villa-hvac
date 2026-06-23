"""House-mode control logic for #2a.

Maps the integration-owned house mode to a KNX preset and applies it to every
preset-controllable thermostat zone. Event-driven: invoked when the house-mode
select changes or the Auto setback switch is turned on — not on a timer, so a
manual thermostat change made afterwards stands until the next mode change.

Respects #10: a zone whose enable switch is OFF is left in building_protection
and never pulled back into a cooling preset.
"""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    HOUSE_MODE_HOME,
    HOUSE_MODE_NIGHT,
    HOUSE_MODES,
    MODE_PRESET,
    PRESET_CONTROLLABLE_EMITTERS,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


def preset_for_mode(mode: str) -> str | None:
    """KNX preset for a house mode (None if the mode is unknown)."""
    return MODE_PRESET.get(mode)


def controllable_zones() -> list[tuple[str, str]]:
    """(zone_id, climate_entity) for zones #2 may drive via presets.

    A zone qualifies if it owns a KNX thermostat and its emitter accepts the
    comfort/standby/economy ladder (fancoil or radiant). Split-AC zones excluded.
    """
    return [
        (zone_id, zone["climate"])
        for zone_id, zone in ZONES.items()
        if zone.get("climate")
        and zone.get("emitter") in PRESET_CONTROLLABLE_EMITTERS
    ]


def _switch_state(
    hass: HomeAssistant, entry: ConfigEntry, unique_suffix: str
) -> str | None:
    """State of one of our switches, resolved by unique_id (None if absent)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_{unique_suffix}"
    )
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return state.state if state else None


def auto_setback_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True unless the global Auto setback switch is explicitly off."""
    return _switch_state(hass, entry, "auto_setback") != STATE_OFF


def is_zone_disabled(hass: HomeAssistant, entry: ConfigEntry, zone_id: str) -> bool:
    """True if the zone's #10 enable switch is off."""
    return _switch_state(hass, entry, f"{zone_id}_enabled") == STATE_OFF


def current_house_mode(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Current house-mode select value (defaults to Home if unavailable)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_house_mode"
    )
    if entity_id and (state := hass.states.get(entity_id)) is not None:
        if state.state in HOUSE_MODES:
            return state.state
    return HOUSE_MODE_HOME


async def apply_house_mode(
    hass: HomeAssistant, entry: ConfigEntry, mode: str, *, force: bool = False
) -> None:
    """Set the mode's preset on every controllable zone (honoring guardrails).

    No-op if Auto setback is off (unless `force`, used when the switch itself is
    being turned on). Zones disabled via #10 are skipped.
    """
    if not force and not auto_setback_enabled(hass, entry):
        return
    preset = preset_for_mode(mode)
    if preset is None:
        _LOGGER.warning("Unknown house mode %s; not applying", mode)
        return
    window = getattr(entry.runtime_data, "window", None)
    paused = window.paused if window is not None else set()
    for zone_id, climate in controllable_zones():
        if is_zone_disabled(hass, entry, zone_id):
            continue  # #10 disabled this zone -> leave it in building_protection
        if zone_id in paused:
            continue  # #4 window open -> keep cooling paused across mode changes
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: climate, ATTR_PRESET_MODE: preset},
            blocking=True,
        )

    # Camere silenziose overlay for the bedrooms (#2b).
    night = getattr(entry.runtime_data, "night", None)
    if night is not None:
        if mode == HOUSE_MODE_NIGHT:
            await night.enter()
        else:
            await night.exit()
