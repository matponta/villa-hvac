"""Preset policies for the Supervisor stack (Phase A migration of #2a/#4/#10).

Each policy reads the immutable HouseState snapshot and returns desired lever
settings (no service calls, no HA state access) so the logic is unit-testable in
isolation. Registered HIGH→LOW priority as `PRESET_POLICIES`:

    disabled_zones (#10)  >  window_pause (#4)  >  house_mode (#2a)

so a #10-disabled or window-paused zone is forced to building_protection and
overrides the house-mode preset, exactly as the legacy controllers did.

These are built and tested here; the live cutover (registering them as the
engine's production policies and removing the legacy direct-write paths) is a
separate, deliberate step.
"""
from __future__ import annotations

from .const import (
    MODE_PRESET,
    PRESET_BUILDING_PROTECTION,
    PRESET_CONTROLLABLE_EMITTERS,
)
from .supervisor import HouseState, ZoneSnapshot, preset_lever, temperature_lever

Desired = dict[str, str | float | None]


def _controllable(zone: ZoneSnapshot) -> bool:
    """A zone #2 may drive via presets: owns a thermostat + a preset emitter."""
    return bool(zone.climate) and zone.emitter in PRESET_CONTROLLABLE_EMITTERS


def disabled_zones_policy(state: HouseState) -> Desired:
    """#10: a disabled zone is held in building_protection (frost-safe).

    Always enforced — the long-term disable is a deliberate user command, not
    part of the Auto-setback automation.
    """
    return {
        preset_lever(z.climate): PRESET_BUILDING_PROTECTION
        for z in state.zones.values()
        if z.climate and not z.enabled
    }


def window_pause_policy(state: HouseState) -> Desired:
    """#4: a window-paused (still-enabled) zone has its cooling paused."""
    if not state.auto_setback:
        return {}
    return {
        preset_lever(z.climate): PRESET_BUILDING_PROTECTION
        for z in state.zones.values()
        if z.climate and z.paused and z.enabled
    }


def house_mode_policy(state: HouseState) -> Desired:
    """#2a: drive each controllable zone to the house mode's preset + setpoint.

    Skips disabled/paused zones (higher-priority policies own those) so we never
    push a setpoint onto a building_protection zone — matching the legacy
    apply_house_mode. No-op when Auto setback is off.
    """
    if not state.auto_setback:
        return {}
    preset = MODE_PRESET.get(state.house_mode)
    if preset is None:
        return {}
    out: Desired = {}
    for z in state.zones.values():
        if not _controllable(z) or not z.enabled or z.paused:
            continue
        out[preset_lever(z.climate)] = preset
        if state.mode_offset is not None and state.house_setpoint is not None:
            out[temperature_lever(z.climate)] = round(
                state.house_setpoint + state.mode_offset, 1
            )
    return out


# Ordered HIGH→LOW priority for the engine's merge_desired.
PRESET_POLICIES = (disabled_zones_policy, window_pause_policy, house_mode_policy)
