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

from datetime import timedelta
import math

from .const import (
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    COOL_GAIN_SOLAR,
    COOL_PULLDOWN,
    DEFAULT_BAND_SLAM,
    DEFAULT_BAND_WIDTH,
    FAN_LEVEL_HYSTERESIS,
    FAN_LEVEL_STEP,
    MODE_PRESET,
    MODEL_ABC_CONF_MIN,
    MODEL_CAP_FAN_STABILITY,
    MODEL_FORGETTING,
    MODEL_K_CONF_MIN,
    MODEL_MAX_A,
    MODEL_MAX_B,
    MODEL_MAX_C,
    MODEL_MAX_K,
    MODEL_MIN_K,
    MODEL_P0_K,
    MODEL_P0_PASSIVE,
    MODEL_RATE_MAX_MIN,
    MODEL_RATE_WINDOW_MIN,
    PRESET_BUILDING_PROTECTION,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_SUMMER,
    SHADING_AZIMUTH_BANDS,
    SHADING_MIN_ELEVATION,
)
from .supervisor import (
    BLOCCO_BLOCK,
    BLOCCO_LEVER,
    BandState,
    DutyState,
    HouseState,
    ParamBounds,
    ThermalParams,
    ZoneSnapshot,
    abc_confidence,
    band_step,
    blend_params,
    capacity_fan,
    cooling_load,
    cover_lever,
    duty_decision,
    estimate_rate,
    fan_lever,
    k_confidence,
    preset_lever,
    rls_capacity_update,
    rls_passive_update,
    seed_params,
    switch_lever,
    temperature_lever,
)

Desired = dict[str, str | float | None]


def _is_cooling_leader(z: ZoneSnapshot) -> bool:
    """A cooling fancoil LEADER: owns a thermostat + its fancoil units, and is not
    a follower (open-space followers like Cucina are driven by their leader).
    Shared by FanBandController, ThermalEstimator and (F3) the planner so the set
    never drifts between them."""
    return bool(
        z.climate and z.emitter == "fancoil" and z.fancoil_units and not z.follows
    )


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
        self._last_fan: dict[str, int] = {}

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
        self._last_fan.clear()
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
            if not _is_cooling_leader(z):
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
                self._last_fan.pop(z.zone_id, None)
                for _fan, manuale in z.fancoil_units:
                    out[switch_lever(manuale)] = "off"  # hand back to AUTO
                continue
            out[temperature_lever(z.climate)] = setpoint
            if phase == "run":
                # F2: use the learned (blended) model where available, else priors.
                a = z.model_a if z.model_a is not None else COOL_GAIN_OUTDOOR
                b = z.model_b if z.model_b is not None else COOL_GAIN_SOLAR
                c = z.model_c if z.model_c is not None else COOL_GAIN_BASE
                k = z.model_k if (z.model_k and z.model_k > 0) else COOL_CAPACITY
                load = cooling_load(z.temp, state.outdoor_temp, state.solar, a=a, b=b, c=c)
                pct = capacity_fan(
                    load, pulldown=COOL_PULLDOWN, capacity=k,
                    fan_min_pct=z.fan_min, step=FAN_LEVEL_STEP,
                    last_level=self._last_fan.get(z.zone_id),
                    hysteresis=FAN_LEVEL_HYSTERESIS,
                )
                self._last_fan[z.zone_id] = pct
            else:  # rest -> min circulation (0 = off, held)
                pct = z.fan_min
                self._last_fan.pop(z.zone_id, None)
            for fan, manuale in z.fancoil_units:
                out[switch_lever(manuale)] = "on"
                out[fan_lever(fan)] = pct
        return out


class ThermalEstimator:
    """F2 OBSERVER: learns the per-room grey-box model online from live data.

    It is NOT a merge controller — it never actuates and writes nothing to HA.
    The engine ticks `observe(state)` every cycle EVEN deploy-dark, so the passive
    params {a,b,c} converge before actuation is ever lit. {a,b,c} are identified on
    w=False windows (no chilled water to the coil -> the -k*u term vanishes); k is
    identified on w=True + fan-held windows (F2b). Estimating dT/dt over a long
    window (not a 30 s diff) is essential given 0.1 C / 30 s quantization noise.
    """

    def __init__(self) -> None:
        self.params: dict[str, ThermalParams] = {}
        self._buf: dict[str, list[tuple]] = {}
        self._last_w: dict[str, bool] = {}
        self._bounds = ParamBounds(
            MODEL_MAX_A, MODEL_MAX_B, MODEL_MAX_C, MODEL_MIN_K, MODEL_MAX_K
        )
        self._window_h = MODEL_RATE_WINDOW_MIN / 60.0
        self._max_window = timedelta(minutes=MODEL_RATE_MAX_MIN)

    @staticmethod
    def _prior() -> ThermalParams:
        return seed_params(
            COOL_GAIN_OUTDOOR, COOL_GAIN_SOLAR, COOL_GAIN_BASE, COOL_CAPACITY,
            p0_passive=MODEL_P0_PASSIVE, p0_k=MODEL_P0_K,
        )

    def model_for(self, zone_id: str) -> ThermalParams:
        """Blended (prior->learned) params for control + diagnostics. Below
        confidence the prior dominates, so control behaves exactly like F1."""
        learned = self.params.get(zone_id)
        if learned is None:
            return self._prior()
        return blend_params(
            learned, self._prior(),
            abc_conf_min=MODEL_ABC_CONF_MIN, k_conf_min=MODEL_K_CONF_MIN,
        )

    def confidence(self, zone_id: str) -> tuple[float, float]:
        """(abc_confidence, k_confidence) in [0,1] for this zone."""
        learned = self.params.get(zone_id)
        if learned is None:
            return 0.0, 0.0
        return (
            abc_confidence(learned, conf_min=MODEL_ABC_CONF_MIN),
            k_confidence(learned, conf_min=MODEL_K_CONF_MIN),
        )

    def observe(self, state: HouseState) -> None:
        """One read-only learning tick over all cooling leaders. Mutates params
        only; returns nothing. Safe to call deploy-dark."""
        if not state.model_learning_enabled:
            return
        for z in state.zones.values():
            if _is_cooling_leader(z):
                self._observe_zone(z, state)

    def _observe_zone(self, z: ZoneSnapshot, state: HouseState) -> None:
        zid = z.zone_id
        self.params.setdefault(zid, self._prior())
        temp, t_out, solar = z.temp, state.outdoor_temp, state.solar
        if temp is None or t_out is None or solar is None:
            return
        # demand for the whole open-space unit (leader + its followers).
        demand_any = z.demand
        for f in state.zones.values():
            if f.follows == zid and f.demand:
                demand_any = True
        if demand_any is None:
            return  # no valve signal -> can't classify the window
        w = (
            bool(demand_any)
            and state.consenso_freddo == "on"
            and state.blocco != BLOCCO_BLOCK
        )
        buf = self._buf.setdefault(zid, [])
        prev_w = self._last_w.get(zid)
        if prev_w is not None and w != prev_w:
            buf.clear()  # a chilled-water edge -> start a fresh homogeneous window
        self._last_w[zid] = w
        now = state.now
        buf.append((now, float(temp), float(t_out), float(solar), z.fan_pct, z.manuale_on))
        while buf and (now - buf[0][0]) > self._max_window:
            buf.pop(0)
        if not buf or (now - buf[0][0]).total_seconds() / 3600.0 < self._window_h:
            return
        rate = estimate_rate([(s[0], s[1]) for s in buf], min_span_h=self._window_h)
        if rate is None:
            return
        n = len(buf)
        mt_out = sum(s[2] for s in buf) / n
        mtemp = sum(s[1] for s in buf) / n
        msolar = sum(s[3] for s in buf) / n
        if not w:
            # F2a: passive {a,b,c} on a no-chilled-water window.
            self.params[zid] = rls_passive_update(
                self.params[zid], dt_dt=rate, t_out=mt_out, temp=mtemp, solar=msolar,
                forgetting=MODEL_FORGETTING, bounds=self._bounds,
            )
        else:
            # F2b: capacity k — only on a HELD, STEADY fan window (manuale on by
            # us + a known %), never from AUTO/unknown or a pull-down transient.
            fans = [s[4] for s in buf]
            held = all(s[5] for s in buf)
            if held and all(f is not None for f in fans) and (
                max(fans) - min(fans) <= MODEL_CAP_FAN_STABILITY
            ):
                u = (sum(fans) / n) / 100.0
                if u > 0:
                    self.params[zid] = rls_capacity_update(
                        self.params[zid], dt_dt=rate, t_out=mt_out, temp=mtemp,
                        solar=msolar, u=u, forgetting=MODEL_FORGETTING,
                        bounds=self._bounds,
                    )
        buf.clear()
        buf.append((now, float(temp), float(t_out), float(solar), z.fan_pct, z.manuale_on))

    # -- persistence (engine drives the Store) --------------------------------
    def load(self, data: dict | None) -> None:
        """Seed params from the persisted store; reject corrupt/unphysical rows
        (a negative k must never reach capacity_fan)."""
        for zid, d in (data or {}).items():
            try:
                p = ThermalParams(
                    a=float(d["a"]), b=float(d["b"]), c=float(d["c"]), k=float(d["k"]),
                    p=tuple(float(x) for x in d["p"]), p_k=float(d["p_k"]),
                    n=int(d.get("n", 0)), n_k=int(d.get("n_k", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                len(p.p) == 9
                and all(math.isfinite(x) for x in (p.a, p.b, p.c, p.k, p.p_k, *p.p))
                and p.a >= 0 and p.b >= 0 and p.c >= 0 and p.k > 0
            ):
                self.params[zid] = p

    def dump(self) -> dict:
        return {
            zid: {
                "a": p.a, "b": p.b, "c": p.c, "k": p.k,
                "p": list(p.p), "p_k": p.p_k, "n": p.n, "n_k": p.n_k,
            }
            for zid, p in self.params.items()
        }
