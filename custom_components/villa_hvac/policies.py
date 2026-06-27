"""Preset policies for the Supervisor stack (Phase A migration of #2a/#4/#10).

Each policy reads the immutable HouseState snapshot and returns desired lever
settings (no service calls, no HA state access) so the logic is unit-testable in
isolation. Registered HIGH→LOW priority as `PRESET_POLICIES`:

    disabled_zones (#10) > window_pause (#4) > free_cool (#5) > house_mode (#2a)

so a #10-disabled, window-paused, or free-cooling-suppressed zone is forced to
building_protection and overrides the house-mode preset.

These are built and tested here; the live cutover (registering them as the
engine's production policies and removing the legacy direct-write paths) is a
separate, deliberate step.
"""
from __future__ import annotations

from .const import (
    FAN_PACING_APPROACH_BAND,
    FAN_PACING_APPROACH_PCT,
    FAN_PACING_MAINTAIN_BAND,
    FAN_PACING_MAINTAIN_PCT,
    MODE_PRESET,
    PRESET_BUILDING_PROTECTION,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_SUMMER,
    SHADING_AZIMUTH_BANDS,
    SHADING_MIN_ELEVATION,
)
from .supervisor import (
    BLOCCO_LEVER,
    DutyState,
    HouseState,
    ZoneSnapshot,
    cover_lever,
    duty_decision,
    fan_lever,
    pacing_decision,
    preset_lever,
    switch_lever,
    temperature_lever,
)

Desired = dict[str, str | float | None]


def _controllable(zone: ZoneSnapshot) -> bool:
    """A zone #2 may drive via presets: owns a thermostat + a preset emitter."""
    return bool(zone.climate) and zone.emitter in PRESET_CONTROLLABLE_EMITTERS


def _free_cooling(state: HouseState) -> bool:
    """True when #5 is suppressing active cooling (summer + cool enough outside)."""
    return (
        state.free_cool_enabled
        and state.season == SEASON_SUMMER
        and state.outdoor_temp is not None
        and state.free_cool_threshold is not None
        and state.outdoor_temp < state.free_cool_threshold
    )


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


def free_cool_policy(state: HouseState) -> Desired:
    """#5: in summer, when it's cool enough outside, suppress the fancoils so the
    house coasts instead of running the PdC. Overrides the house-mode preset on
    the cooling (fancoil) zones; releases (no opinion) otherwise. Threshold-only
    for now — outdoor temp moves slowly, so per-cycle flapping is unlikely.
    """
    if not _free_cooling(state):
        return {}
    return {
        preset_lever(z.climate): PRESET_BUILDING_PROTECTION
        for z in state.zones.values()
        if z.climate and z.emitter == "fancoil" and z.enabled and not z.paused
    }


def precool_policy(state: HouseState) -> Desired:
    """#9 planner: when a hot peak is forecast (state.precool), bank coolth by
    nudging the fancoil cooling zones' setpoints below the normal target. Gated
    by the duty switch (part of #9), summer only, and skipped while free-cooling
    (cool outside -> no need). Overrides house_mode's setpoint (higher priority).
    """
    if not state.duty_enabled or not state.precool or state.season != SEASON_SUMMER:
        return {}
    if _free_cooling(state):
        return {}
    if state.house_setpoint is None or state.mode_offset is None:
        return {}
    if state.precool_offset is None:
        return {}
    target = state.house_setpoint + state.mode_offset - state.precool_offset
    return {
        temperature_lever(z.climate): round(target, 1)
        for z in state.zones.values()
        if _controllable(z) and z.emitter == "fancoil" and z.enabled and not z.paused
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
    free_cooling = _free_cooling(state)
    out: Desired = {}
    for z in state.zones.values():
        if not _controllable(z) or not z.enabled or z.paused:
            continue
        if free_cooling and z.emitter == "fancoil":
            continue  # #5 suppresses this zone -> don't push a setpoint onto BP
        out[preset_lever(z.climate)] = preset
        if state.mode_offset is not None and state.house_setpoint is not None:
            out[temperature_lever(z.climate)] = round(
                state.house_setpoint + state.mode_offset, 1
            )
    return out


def _azimuth_in_band(azimuth: float, orientation: str) -> bool:
    """True if the sun's compass azimuth is within the facade's band."""
    band = SHADING_AZIMUTH_BANDS.get(orientation)
    if band is None:
        return False
    lo, hi = band
    if lo < hi:
        return lo <= azimuth < hi
    return azimuth >= lo or azimuth < hi  # wraps through 0/360 (north)


def shading_policy(state: HouseState) -> Desired:
    """#6: close a sun-facing cover when the sun is on its facade and it's bright.

    Summer only; requires the sun above the horizon, its azimuth in the cover's
    orientation band, and solar radiation over the threshold. Acts on the cover
    lever (independent of the preset levers). Releases (no opinion) otherwise —
    we don't force-reopen, so existing morning/dusk cover automations and the
    user keep control.
    """
    if not state.shading_enabled or state.season != SEASON_SUMMER:
        return {}
    if state.sun_elevation is None or state.sun_elevation <= SHADING_MIN_ELEVATION:
        return {}
    if state.sun_azimuth is None:
        return {}
    if state.solar is None or state.shading_solar_threshold is None:
        return {}
    if state.solar < state.shading_solar_threshold:
        return {}
    return {
        cover_lever(cover.entity_id): "closed"
        for cover in state.covers
        if _azimuth_in_band(state.sun_azimuth, cover.orientation)
    }


# Ordered HIGH→LOW priority for the engine's merge_desired:
# #10 disable > #4 window > #5 free-cool > #9 pre-cool > #2a house mode.
# (Shading acts on the independent cover lever, so its order is immaterial.)
PRESET_POLICIES = (
    disabled_zones_policy,
    window_pause_policy,
    free_cool_policy,
    precool_policy,
    house_mode_policy,
)

# The full stack the engine runs (presets + the cover-lever shading policy).
POLICIES = (*PRESET_POLICIES, shading_policy)


def _comfort_breach(state: HouseState) -> bool:
    """True if any zone is above the duty comfort-max (overrides the timer)."""
    if state.duty_comfort_max is None:
        return False
    return any(
        z.temp is not None and z.temp > state.duty_comfort_max
        for z in state.zones.values()
    )


class DutyController:
    """#9 central duty-cycle (stateful): cap the continuous cooling stint, then
    force a cooloff via the Consenso BLOCCO, then release — synchronizing the
    villa (all rooms run, then all rest). The decision logic is the pure
    `duty_decision`; this only holds the cross-cycle `DutyState` and reads the
    enable/params from the snapshot. Returns no opinion while disabled.
    """

    def __init__(self) -> None:
        self._duty = DutyState()

    def __call__(self, state: HouseState) -> Desired:
        if (
            not state.duty_enabled
            or state.duty_max_stint is None
            or state.duty_cooloff is None
        ):
            self._duty = DutyState()  # forget timers while disabled
            return {}
        cooling = state.consenso_freddo == "on"
        at_peak = (
            state.outdoor_temp is not None
            and state.duty_peak_outdoor is not None
            and state.outdoor_temp >= state.duty_peak_outdoor
        )
        self._duty, blocco = duty_decision(
            cooling,
            _comfort_breach(state),
            state.now,
            self._duty,
            state.duty_max_stint,
            state.duty_cooloff,
            at_peak=at_peak,
            precool=state.precool,
        )
        return {BLOCCO_LEVER: blocco}


class FanPacingController:
    """#3 fan pacing (stateful): within a cooling run, hold each cooling fancoil
    room's fan in MANUAL at a paced speed (two-phase pull-down → maintain) so the
    valve stops bang-banging. Skips a bedroom while camere silenziose (#2b) owns
    it. Releases (manuale off) a room it previously paced once it stops cooling.
    Off while disabled. Speeds/bands are tunable post the live held-low-fan test.
    """

    def __init__(self) -> None:
        self._phase: dict[str, str] = {}

    def __call__(self, state: HouseState) -> Desired:
        if not state.fan_pacing_enabled:
            self._phase.clear()
            return {}
        target = None
        if state.house_setpoint is not None and state.mode_offset is not None:
            target = state.house_setpoint + state.mode_offset
        cooling_run = state.consenso_freddo == "on"
        out: Desired = {}
        for z in state.zones.values():
            if z.emitter != "fancoil" or not z.fancoil or not z.manuale:
                continue
            if z.bedroom and state.night_active:
                self._phase.pop(z.zone_id, None)  # #2b owns the bedroom fan
                continue
            pacing = (
                cooling_run and z.demand and z.enabled and not z.paused
                and z.temp is not None and target is not None
            )
            if pacing:
                phase, pct = pacing_decision(
                    self._phase.get(z.zone_id, "approach"),
                    z.temp - target,
                    approach_band=FAN_PACING_APPROACH_BAND,
                    maintain_band=FAN_PACING_MAINTAIN_BAND,
                    approach_pct=FAN_PACING_APPROACH_PCT,
                    maintain_pct=FAN_PACING_MAINTAIN_PCT,
                )
                self._phase[z.zone_id] = phase
                out[switch_lever(z.manuale)] = "on"
                out[fan_lever(z.fancoil)] = pct
            elif z.zone_id in self._phase:
                self._phase.pop(z.zone_id)
                out[switch_lever(z.manuale)] = "off"  # release what we paced
        return out
