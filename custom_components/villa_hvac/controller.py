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

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DEFAULT_FAN_MIN,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_NIGHT,
    HOUSE_MODES,
    MODE_PRESET,
    OPT_FAN_MIN,
    OPT_SEASON,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_OFFSET_DEFAULTS,
    SEASON_OFFSET_OPTS,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


def preset_for_mode(mode: str) -> str | None:
    """KNX preset for a house mode (None if the mode is unknown)."""
    return MODE_PRESET.get(mode)


def current_season(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Season for offset selection: options override, else auto from the PdC mode."""
    forced = entry.options.get(OPT_SEASON)
    if forced in (SEASON_SUMMER, SEASON_WINTER):
        return forced
    state = hass.states.get(SEASON_REFERENCE_CLIMATE)
    if state is not None and state.state == "heat":
        return SEASON_WINTER
    return SEASON_SUMMER


def mode_offset(hass: HomeAssistant, entry: ConfigEntry, mode: str) -> float | None:
    """Setpoint offset for a mode (season-aware, options-editable).

    Casa -> +0; Vacanza -> None (building_protection, no setpoint). Via/Notte use
    the current season's editable offset.
    """
    if mode == HOUSE_MODE_HOME:
        return 0.0
    if mode not in (HOUSE_MODE_AWAY, HOUSE_MODE_NIGHT):
        return None  # Vacanza / unknown
    season = current_season(hass, entry)
    default = SEASON_OFFSET_DEFAULTS[season][mode]
    try:
        return float(entry.options.get(SEASON_OFFSET_OPTS[season][mode], default))
    except (TypeError, ValueError):
        return default


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


def _number_value(
    hass: HomeAssistant, entry: ConfigEntry, unique_suffix: str
) -> float | None:
    """Value of one of our number entities, resolved by unique_id (None if absent
    or non-numeric)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_{unique_suffix}"
    )
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def auto_setback_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True unless the global Auto setback switch is explicitly off."""
    return _switch_state(hass, entry, "auto_setback") != STATE_OFF


def shade_blocked(hass: HomeAssistant, entry: ConfigEntry, zone: str) -> bool:
    """True when a room's manual shade-block override switch (#6) is on."""
    return _switch_state(hass, entry, f"shade_block_{zone}") == STATE_ON


def shade_position(
    hass: HomeAssistant, entry: ConfigEntry, zone: str
) -> int | None:
    """Per-room shade target position (#6), or None to use the house default."""
    value = _number_value(hass, entry, f"shade_position_{zone}")
    return int(value) if value is not None else None


def fan_min(hass: HomeAssistant, entry: ConfigEntry, zone: str) -> int:
    """Per-zone min-circulation fan % (#3 v2): the per-zone override number, else
    the global default. 0 = fan off during REST."""
    value = _number_value(hass, entry, f"fan_min_{zone}")
    if value is None:
        try:
            value = float(entry.options.get(OPT_FAN_MIN, DEFAULT_FAN_MIN))
        except (TypeError, ValueError):
            value = DEFAULT_FAN_MIN
    return max(0, min(100, int(value)))


def supervisor_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True only when the master Supervisor switch is on (strict deploy-dark).

    The whole engine — including the migrated #2/#4/#10 — is a no-op until this
    is turned on; then the entire organism lights up at once.
    """
    return _switch_state(hass, entry, "supervisor") == STATE_ON


def duty_cycle_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #9 duty-cycle switch is on (opt-in, on top of the master)."""
    return _switch_state(hass, entry, "duty_cycle") == STATE_ON


def fan_pacing_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #3 fan-pacing switch is on (opt-in, on top of the master)."""
    return _switch_state(hass, entry, "fan_pacing") == STATE_ON


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


def current_house_setpoint(hass: HomeAssistant, entry: ConfigEntry) -> float | None:
    """House comfort setpoint from the number entity (None if unavailable)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_house_setpoint"
    )
    if entity_id and (state := hass.states.get(entity_id)) is not None:
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None
    return None


async def apply_house_mode(
    hass: HomeAssistant, entry: ConfigEntry, mode: str, *, force: bool = False
) -> None:
    """Trigger a supervisor pass for `mode` + the camere-silenziose overlay.

    Preset/setpoint actuation now belongs to the engine's policy stack
    (`house_mode_policy` #2a, with #4/#10 as higher-priority overrides), so this
    just nudges the engine to recompute against current state. Strict
    deploy-dark: a no-op while the master Supervisor switch is off. `force` is
    kept for call-site compatibility — the engine reads live Auto-setback state,
    so no forced write is needed.
    """
    engine = getattr(entry.runtime_data, "engine", None)
    if engine is None or not engine.enabled:
        return
    await engine.request_run()

    # Camere silenziose overlay for the bedrooms (#2b) on Notte transitions.
    night = getattr(entry.runtime_data, "night", None)
    if night is not None:
        if mode == HOUSE_MODE_NIGHT:
            await night.enter()
        else:
            await night.exit()
