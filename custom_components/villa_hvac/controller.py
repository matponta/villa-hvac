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
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    COMFORT_FLOOR_MAX,
    COMFORT_FLOOR_MIN,
    COMFORT_FLOOR_OFFSET,
    DEFAULT_COMFORT_FLOOR,
    DEFAULT_FAN_MIN,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_NIGHT,
    HOUSE_MODES,
    MODE_PRESET,
    OPT_COMFORT_FLOOR,
    OPT_FAN_MIN,
    OPT_SEASON,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_OFFSET_DEFAULTS,
    SEASON_OFFSET_OPTS,
    SEASON_REFERENCE_CLIMATE,
    SEASON_STAGIONE_SENSOR,
    SEASON_STAGIONE_SUMMER,
    SEASON_STAGIONE_WINTER,
    SEASON_SUMMER,
    SEASON_WINTER,
    SETPOINT_OFFSET_MAX,
    SETPOINT_OFFSET_MIN,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


def preset_for_mode(mode: str) -> str | None:
    """KNX preset for a house mode (None if the mode is unknown)."""
    return MODE_PRESET.get(mode)


def current_season(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Season for offset + cooling gating: options override, else auto-detected.

    Corroborates two signals so a single unavailable KNX climate can't silently
    flip the organism into summer mid-winter (the s5a_villa_modo failure class
    CLAUDE.md warns about): an AFFIRMATIVE hvac mode on the reference thermostat
    (heat/cool) wins; when that is inconclusive (unavailable/off/unknown) fall back
    to the robust s5a stagione sensor (Estate/Inverno); only if neither is
    conclusive default to summer (the cooling season here — and a wrong winter
    guess merely warms a setback offset, since the cooling controllers separately
    gate on affirmative demand).
    """
    forced = entry.options.get(OPT_SEASON)
    if forced in (SEASON_SUMMER, SEASON_WINTER):
        return forced
    state = hass.states.get(SEASON_REFERENCE_CLIMATE)
    if state is not None:
        if state.state == "heat":
            return SEASON_WINTER
        if state.state == "cool":
            return SEASON_SUMMER
    stagione = hass.states.get(SEASON_STAGIONE_SENSOR)
    if stagione is not None:
        if stagione.state == SEASON_STAGIONE_WINTER:
            return SEASON_WINTER
        if stagione.state == SEASON_STAGIONE_SUMMER:
            return SEASON_SUMMER
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


def setpoint_offset(hass: HomeAssistant, entry: ConfigEntry, zone: str) -> float:
    """Per-zone comfort offset (#2): °C added to this zone's base center
    (house_setpoint + mode_offset). Negative = cooler than the house. 0.0 when
    the per-zone number is unset. Clamped to the safe band."""
    value = _number_value(hass, entry, f"setpoint_offset_{zone}")
    if value is None:
        return 0.0
    return max(SETPOINT_OFFSET_MIN, min(SETPOINT_OFFSET_MAX, float(value)))


def comfort_floor(
    hass: HomeAssistant, entry: ConfigEntry, house_setpoint: float | None
) -> float:
    """Comfort floor (°C) — the lower bound on the band center (F4c Phase 1).

    Explicit `OPT_COMFORT_FLOOR` if set, else the dynamic default
    `house_setpoint − COMFORT_FLOOR_OFFSET` (so it tracks the slider until the
    owner pins an absolute value). Clamped to [COMFORT_FLOOR_MIN, COMFORT_FLOOR_MAX].
    """
    raw = entry.options.get(OPT_COMFORT_FLOOR)
    base: float | None = None
    if raw is not None:
        try:
            base = float(raw)
        except (TypeError, ValueError):
            base = None
    if base is None:
        base = (
            house_setpoint - COMFORT_FLOOR_OFFSET
            if house_setpoint is not None else DEFAULT_COMFORT_FLOOR
        )
    return max(COMFORT_FLOOR_MIN, min(COMFORT_FLOOR_MAX, base))


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


def pv_bias_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the PV/energy-aware pre-cool switch is on (opt-in). It executes via
    the band center, so it also needs fan_pacing on to have any effect."""
    return _switch_state(hass, entry, "pv_bias") == STATE_ON


def unified_planner_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the F4c unified-planner switch is on (opt-in, deploy-dark). It lets
    the forecast schedule DRIVE the band center for planner-eligible rooms; it
    executes through the band, so it also needs fan_pacing on to have any effect."""
    return _switch_state(hass, entry, "unified_planner") == STATE_ON


def split_ac_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #6 split-AC trio switch is on (opt-in, on top of the master)."""
    return _switch_state(hass, entry, "split_ac") == STATE_ON


def free_air_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #3 free-air / windows-open switch is on (opt-in).

    A manual house-wide "I've opened the windows" flag: the cooled fancoil zones
    are treated as window-paused so the AC doesn't fight the open air. Same
    mechanism as #4 (a window contact opening), just user-triggered."""
    return _switch_state(hass, entry, "free_air") == STATE_ON


def vmc_boost_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #5 VMC auto-boost switch is on (opt-in, on top of the master).

    NOTE the actuator is the separate KNX `switch.vmc_boost`; this is the
    automation opt-in `switch.vmc_auto`, so the names don't collide."""
    return _switch_state(hass, entry, "vmc_auto") == STATE_ON


def free_cool_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #5 free-cooling switch is on (opt-in, on top of the master).

    v0.53.0: replaced the always-on options toggle — the owner wants explicit
    control over the auto-coast (cool outside -> suppress the fancoils)."""
    return _switch_state(hass, entry, "free_cool") == STATE_ON


def is_zone_disabled(hass: HomeAssistant, entry: ConfigEntry, zone_id: str) -> bool:
    """True if the zone's #10 enable switch is off."""
    return _switch_state(hass, entry, f"{zone_id}_enabled") == STATE_OFF


def return_precond_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when the #8 return-precond opt-in switch is on (on top of the master)."""
    return _switch_state(hass, entry, "return_precond") == STATE_ON


def return_armed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True when a #8 return-home ETA is armed."""
    return _switch_state(hass, entry, "return_armed") == STATE_ON


def _entity_id(
    hass: HomeAssistant, entry: ConfigEntry, domain: str, unique_suffix: str
) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_{unique_suffix}"
    )


def return_date(hass: HomeAssistant, entry: ConfigEntry):
    """The #8 return date (a `datetime.date`), or None if unset/invalid."""
    from homeassistant.util import dt as dt_util  # local: keep module import-light

    entity_id = _entity_id(hass, entry, "date", "return_date")
    if not entity_id or (state := hass.states.get(entity_id)) is None:
        return None
    if state.state in ("unavailable", "unknown", ""):
        return None
    return dt_util.parse_date(state.state)


def return_daypart(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The #8 return daypart (mattino/pomeriggio/sera), or None if unset."""
    entity_id = _entity_id(hass, entry, "select", "return_daypart")
    if not entity_id or (state := hass.states.get(entity_id)) is None:
        return None
    return state.state if state.state not in ("unavailable", "unknown", "") else None


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
    """House comfort setpoint from the number entity (None if unavailable).

    Rejects NaN/inf: this value flows straight into the band center and out to a
    KNX set_temperature, so a non-finite helper value must never propagate (the
    write-side counterpart to the _num/coordinator isfinite guards)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_house_setpoint"
    )
    if entity_id and (state := hass.states.get(entity_id)) is not None:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


async def apply_house_mode(
    hass: HomeAssistant, entry: ConfigEntry, mode: str, *, force: bool = False
) -> None:
    """Trigger a supervisor pass so the mode change actuates.

    Preset/setpoint actuation belongs to the engine's policy stack
    (`house_mode_policy` #2a, with #4/#10 as higher-priority overrides) and the
    camere-silenziose overlay (#2b) is now a merge controller
    (`NightSilenceController`, C1) that derives its active state from the house
    mode — so this just nudges the engine to recompute against current state.
    Strict deploy-dark: a no-op while the master Supervisor switch is off. `mode`/
    `force` are kept for call-site compatibility; the engine reads live state.
    """
    engine = getattr(entry.runtime_data, "engine", None)
    if engine is None or not engine.enabled:
        return
    await engine.request_run()
