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
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    COOL_GAIN_SOLAR,
    COOL_PULLDOWN,
    DEFAULT_BAND_SLAM,
    DEFAULT_BAND_WIDTH,
    FAN_LEVEL_STEP,
    MODE_PRESET,
    PRESET_BUILDING_PROTECTION,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_SUMMER,
    SHADING_AZIMUTH_BANDS,
    SHADING_MIN_ELEVATION,
)
from .supervisor import (
    BLOCCO_LEVER,
    BandState,
    DutyState,
    HouseState,
    ZoneSnapshot,
    band_step,
    capacity_fan,
    cooling_load,
    cover_lever,
    duty_decision,
    fan_lever,
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
    """#6: drive a sun-facing cover to its per-room shade position when the sun is
    on its facade and it's bright.

    Summer only; requires the sun above the horizon, its azimuth in the cover's
    orientation band, and solar radiation over the threshold. Each cover is moved
    to its per-room `target_position` (HA cover position: 0 = fully down,
    100 = open), falling back to the house default — instead of slamming it fully
    shut. A room with its manual block override on is skipped. Acts on the cover
    lever (independent of the preset levers). Releases (no opinion) otherwise — we
    don't force-reopen, so existing morning/dusk cover automations and the user
    keep control.
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
    out: Desired = {}
    for cover in state.covers:
        if cover.blocked or not _azimuth_in_band(state.sun_azimuth, cover.orientation):
            continue
        position = (
            cover.target_position
            if cover.target_position is not None
            else state.shading_default_position
        )
        if position is None:
            continue
        out[cover_lever(cover.entity_id)] = int(position)
    return out


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

    @property
    def duty(self) -> DutyState:
        """The live cross-cycle duty state (read by the #11 plan view)."""
        return self._duty

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


class FanBandController:
    """#3 v2 (stateful): comfort-band setpoint control + capacity-matched fan.

    Per cooling fancoil LEADER zone (it owns its open-space fancoils, e.g.
    living_room drives Salotto + Cucina together), runs a wide-hysteresis loop:
    RUN slams the setpoint to center-A (valve forced open) and holds the fan at a
    capacity-matched speed; REST slams it to center+A (valve closed) and drops the
    fan to the min-circulation level; flips at center±B/2. This replaces the
    KNX thermostat's too-narrow band, so the valve stops bang-banging and the fan
    runs steady & quiet. Center = house target (− pre-cool offset when #9 says so).

    Skips bedrooms while camere silenziose (#2b) owns them, disabled/paused zones,
    free-cooling, and non-summer. Releases (manuale off → AUTO) when ineligible.
    Opt-in via switch.fan_pacing. Followers (kitchen, rack) are driven by their
    leader's fancoil list, not iterated here.
    """

    def __init__(self) -> None:
        self._states: dict[str, BandState] = {}

    def _release_all(self, state: HouseState) -> Desired:
        """Hand every fan we were holding back to AUTO (manuale off), once. Levers
        not present in `desired` are NOT auto-released by the engine, so on
        disable/season-flip we must emit the releases explicitly."""
        if not self._states:
            return {}
        out: Desired = {}
        for zid in self._states:
            z = state.zones.get(zid)
            if z:
                for _fan, manuale in z.fancoil_units:
                    out[switch_lever(manuale)] = "off"
        self._states.clear()
        return out

    def __call__(self, state: HouseState) -> Desired:
        if not state.fan_pacing_enabled or state.season != SEASON_SUMMER:
            return self._release_all(state)
        center_base = None
        if state.house_setpoint is not None and state.mode_offset is not None:
            center_base = state.house_setpoint + state.mode_offset
        free_cool = _free_cooling(state)
        band = state.band_width if state.band_width is not None else DEFAULT_BAND_WIDTH
        slam = state.band_slam if state.band_slam is not None else DEFAULT_BAND_SLAM
        out: Desired = {}
        for z in state.zones.values():
            if z.follows or z.emitter != "fancoil" or not z.climate or not z.fancoil_units:
                continue  # followers + non-fancoil are not leaders
            if z.bedroom and state.night_active:
                self._states.pop(z.zone_id, None)  # #2b camere silenziose owns it
                continue
            eligible = (
                center_base is not None
                and z.enabled and not z.paused and not free_cool
            )
            center = center_base
            if eligible and state.precool and state.duty_enabled and (
                state.precool_offset is not None
            ):
                center = center_base - state.precool_offset
            phase, setpoint = band_step(
                self._states.get(z.zone_id, BandState()).phase,
                eligible=bool(eligible),
                temp=z.temp,
                center=center if eligible else None,
                band=band,
                slam=slam,
            )
            self._states[z.zone_id] = BandState(phase=phase)
            if phase == "released":
                for _fan, manuale in z.fancoil_units:
                    out[switch_lever(manuale)] = "off"  # hand back to AUTO
                continue
            out[temperature_lever(z.climate)] = setpoint
            if phase == "run":
                load = cooling_load(
                    z.temp, state.outdoor_temp, state.solar,
                    a=COOL_GAIN_OUTDOOR, b=COOL_GAIN_SOLAR, c=COOL_GAIN_BASE,
                )
                pct = capacity_fan(
                    load, pulldown=COOL_PULLDOWN, capacity=COOL_CAPACITY,
                    fan_min_pct=z.fan_min, step=FAN_LEVEL_STEP,
                )
            else:  # rest -> min circulation (0 = off, held)
                pct = z.fan_min
            for fan, manuale in z.fancoil_units:
                out[switch_lever(manuale)] = "on"
                out[fan_lever(fan)] = pct
        return out
