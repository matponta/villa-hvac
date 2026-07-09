"""Pure planner (C2 split): forecast run-plan, per-room forward simulation +
pre-cool scheduler, solar-forecast curve, house-load regime, and the #11 plan
view. The home for the F4c unified plan_center_schedule()."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math

from .arbiter import BLOCCO_BLOCK, BLOCCO_RELEASE, cover_lever, temperature_lever
from .control_law import (
    PRECOOL_BANK,
    PRECOOL_COAST,
    DutyState,
    band_step,
    compose_center,
    cooling_effectiveness,
    cooling_load,
    effective_pulldown,
    energy_precool_decision,
    run_fan_pct,
    run_rest_durations,
)
from .model import (
    HouseState,
    ZoneSnapshot,
    _SEASON_SUMMER,
    _is_cooling_leader,
    _is_free_cooling,
    active_cooling_leaders,
)
from .returnhome import ReturnRoom, return_lead_time




# --- #9 forecast run-window planner (pure) -----------------------------------


@dataclass(frozen=True)
class RunPlan:
    """Forecast-derived plan for the cooling run window (re-planned each refresh)."""

    precool: bool = False               # bank coolth now ahead of a coming peak
    forecast_peak: float | None = None  # max forecast temp over the lookahead
    peak_eta: timedelta | None = None   # time until that peak



def plan_run(
    forecast: list[tuple[datetime, float]],
    now: datetime,
    current_outdoor: float | None,
    *,
    peak_threshold: float,
    lookahead: timedelta,
    margin: float,
) -> RunPlan:
    """Build the run plan over the lookahead horizon (e.g. 12 h).

    Find the forecast peak in [now, now+lookahead]. Pre-cool when a HOT peak
    (>= peak_threshold) is still ahead AND it is currently at least `margin`
    cooler than that peak — i.e. bank coolth in the cool hours, and taper as the
    peak nears (once we're within `margin` of it, stop; peak-skip takes over).
    The long lookahead lets a high-mass house start early; the margin keeps it
    from pre-cooling all day.
    """
    pk = peak_window(forecast, now, lookahead)
    if pk is None:
        return RunPlan()
    peak_when, peak_temp = pk
    eta = peak_when - now
    if peak_temp < peak_threshold or current_outdoor is None:
        return RunPlan(precool=False, forecast_peak=peak_temp, peak_eta=eta)
    precool = peak_when > now and (peak_temp - current_outdoor) >= margin
    return RunPlan(precool=precool, forecast_peak=peak_temp, peak_eta=eta)



def peak_window(
    forecast: list[tuple[datetime, float]], now: datetime, lookahead: timedelta
) -> tuple[datetime, float] | None:
    """The hottest forecast point in [now, now+lookahead] (argmax). The SINGLE
    peak definition shared by plan_run (the scalar #9 flag) and the per-room
    forward simulation, so they never disagree on where the peak is."""
    window = [
        (when, t)
        for (when, t) in forecast
        if now <= when <= now + lookahead and t is not None
    ]
    if not window:
        return None
    return max(window, key=lambda wt: wt[1])



@dataclass(frozen=True)
class ZonePlan:
    """Per-zone slice of the plan: where it is and where the plan wants it."""

    zone_id: str
    name: str
    temp: float | None
    target: float | None        # planned setpoint (from the policy stack), if any
    demand: bool | None         # EV FAN valve open = actually cooling now
    enabled: bool
    paused: bool



@dataclass(frozen=True)
class PlanView:
    """The next-12h plan, projected from one HouseState + RunPlan + DutyState."""

    summary: str                # one-word regime (the sensor state)
    season: str | None
    house_mode: str | None
    cooling: bool               # consenso_freddo on right now
    free_cool: bool             # #5 coasting (cool enough outside)
    precool: bool               # #9 banking coolth ahead of a peak
    at_peak: bool               # hot now -> peak-skip (let the PdC run)
    forecast_peak: float | None
    peak_eta: timedelta | None
    house_setpoint: float | None
    effective_setpoint: float | None   # base + mode offset
    precool_setpoint: float | None     # effective - precool offset (while pre-cooling)
    in_cooloff: bool            # #9 duty currently resting (BLOCCO block)
    cooloff_until: datetime | None
    stint_start: datetime | None
    stint_elapsed: timedelta | None
    stint_cap: timedelta | None
    blocco: str | None          # current central-block state
    blocco_desired: str | None  # what the duty regime wants the block to be
    duty_enabled: bool
    zones: tuple[ZonePlan, ...]
    covers_closing: tuple[str, ...]   # covers the shading policy is closing now
    forecast: tuple[tuple[datetime, float], ...]  # windowed curve for the timeline
    # F3a: house regime + load aggregates (set by the engine via replace()).
    regime: str | None = None
    g_house: float | None = None
    k_house: float | None = None
    load_ratio: float | None = None
    # F3b: per-room 12h trajectories (set by the engine via replace()).
    room_trajectories: tuple = ()
    solar_model: str = "flat"   # F4a: "flat" prior vs "forecast" (sun×cloud model)
    # STORY_SEFF §1.4: the solar domain the room sims ran in — "seff" (per-zone
    # facade curves) or "ghi" (house curve). Divergence from the live band's
    # domain must be visible, never silent.
    solar_domain: str = "ghi"
    # F4c Phase 1: per-leader band-center composition (base + active feature +
    # floor) for observability — zone_id -> CenterComposition. Computed read-only
    # every cycle (even deploy-dark) so the composition is visible before go-live.
    center_compositions: dict = field(default_factory=dict)
    # F4c Phase 5: the unified 12h band-center REFERENCE schedule (Track B),
    # PLAN-ONLY — observable, drives nothing (Phase 6 wires it behind the switch).
    center_schedule: "CenterSchedule | None" = None
    # R4 (Tier-1): per-optimizer {enabled, active, inert_reason} — set by the
    # engine via replace(). Populated even deploy-dark so live validation can see
    # WHY a feature did nothing.
    feature_graph: tuple = ()



# --- R4 (Tier-1): feature graph — observability before behavior --------------
# One row per optimizer: is its opt-in ON (`enabled`), is it doing something THIS
# cycle (`active`), and if not — a one-line `inert_reason`. So a live operator can
# tell "did nothing because the switch is off" from "switch on but no cooling
# demand" from "switch on but supervisor (master) is off". Pure: the `enabled`
# bits come from the engine (switch/config reads — some need `hass`); active +
# reason are derived here from the already-computed HouseState + PlanView.

# Display order (the sensor lists rows in this order).
FEATURE_ORDER: tuple[str, ...] = (
    "fan_pacing", "duty_cycle", "regime", "precool", "free_cool",
    "comfort_windows", "pv_bias", "unified_planner", "shading", "night",
)


@dataclass(frozen=True)
class FeatureStatus:
    """One optimizer's live status for `sensor.hvac_plan.feature_graph`."""

    feature: str
    enabled: bool           # the opt-in switch/config for this feature is on
    active: bool            # it is actually doing something this cycle
    inert_reason: str | None  # None when active; else why it is doing nothing


def build_feature_graph(
    state: HouseState,
    plan: PlanView,
    *,
    master_on: bool,
    enabled: dict[str, bool],
) -> tuple[FeatureStatus, ...]:
    """Project each optimizer's {enabled, active, inert_reason} (pure).

    Precedence of the inert reason: a disabled switch reads "disabled" (most
    specific); an enabled-but-master-off feature reads "supervisor off" (the
    dominant deploy-dark reason); otherwise the feature's own gate.
    """
    summer = state.season == _SEASON_SUMMER
    has_leaders = bool(active_cooling_leaders(state))

    def row(feature: str, is_active: bool, reason: str) -> FeatureStatus:
        if not enabled.get(feature, False):
            return FeatureStatus(feature, False, False, "disabled")
        if not master_on:
            return FeatureStatus(feature, True, False, "supervisor off")
        if is_active:
            return FeatureStatus(feature, True, True, None)
        return FeatureStatus(feature, True, False, reason)

    zones = state.zones.values()
    rows = {
        "fan_pacing": row(
            "fan_pacing",
            summer and has_leaders,
            "not cooling season" if not summer else "no active cooling zone",
        ),
        "duty_cycle": row(
            "duty_cycle",
            plan.in_cooloff,
            "peak: PdC free-runs" if plan.at_peak
            else ("not cooling season" if not summer else "cooling within stint cap"),
        ),
        "regime": row(
            "regime",
            plan.regime == "medium",
            f"{plan.regime or 'unknown'} regime (coalesce only in medium)",
        ),
        "precool": row("precool", plan.precool, "no peak ahead to bank for"),
        "free_cool": row(
            "free_cool",
            plan.free_cool,
            "not cooling season" if not summer else "outdoor not cool enough",
        ),
        "comfort_windows": row(
            "comfort_windows",
            any(z.comfort_relax > 0 for z in zones),
            "inside every comfort window",
        ),
        "pv_bias": row(
            "pv_bias",
            state.pv_mode in ("bank", "coast"),
            "no PV surplus to bank",
        ),
        "unified_planner": row(
            "unified_planner",
            any(z.planner_driven for z in zones),
            "no planner-driven room (none eligible / schedule stale)",
        ),
        "shading": row(
            "shading",
            bool(plan.covers_closing),
            "not cooling season" if not summer else "sun below shading threshold",
        ),
        "night": row("night", state.night_active, "not night / no bedroom setback"),
    }
    return tuple(rows[f] for f in FEATURE_ORDER)



# --- F3a: house load index + regime selector (pure) --------------------------
# Aggregate per-room heat-gain G and capacity k into a house regime: PEAK (no
# coalescing headroom — lean on shading/precool), MEDIUM (coalesce demand into
# shared run/rest windows), LOW (free-cool). On F1 priors the ratio is mis-scaled
# (it reads ~0.4 at the verified 0-net 34°C peak), so the ratio path is trusted
# ONLY for zones whose k has converged; PEAK otherwise keys off at_peak alone.

REGIME_PEAK = "peak"

REGIME_MEDIUM = "medium"

REGIME_LOW = "low"



@dataclass(frozen=True)
class HouseLoad:
    """Aggregate cooling demand vs capacity across the converged cooling rooms."""

    g_house: float = 0.0          # Σ heat-gain G over converged-k leaders (°C/h)
    k_house: float = 0.0          # Σ capacity k over the same (°C/h at full fan)
    load_ratio: float = 0.0       # g_house / k_house (≥1 ⇒ gain-limited)
    n_eligible: int = 0           # leaders counted toward the ratio (converged k)
    per_zone: dict = field(default_factory=dict)  # zone_id -> (G, k) for display



def house_load_index(
    state: HouseState, *,
    default_a: float, default_b: float, default_c: float, default_capacity: float,
    k_conf_min: float,
) -> HouseLoad:
    """Build the house load index. A leader contributes to the RATIO only when its
    k is confidence-converged (so the ratio isn't mis-scaled on priors); all
    eligible leaders appear in per_zone for display."""
    free = _is_free_cooling(state)
    g_sum = k_sum = 0.0
    n = 0
    per: dict[str, tuple[float, float]] = {}
    for z in state.zones.values():
        if not _is_cooling_leader(z) or not z.enabled or z.paused or free:
            continue
        if z.bedroom and state.night_active:
            continue
        a = z.model_a if z.model_a is not None else default_a
        b = z.model_b if z.model_b is not None else default_b
        c = z.model_c if z.model_c is not None else default_c
        g = max(0.0, cooling_load(z.temp, state.outdoor_temp, z.s_eff, a=a, b=b, c=c))
        converged = (
            z.model_k_confidence is not None
            and z.model_k_confidence >= k_conf_min
            and z.model_k is not None and z.model_k > 0
        )
        k = z.model_k if converged else default_capacity
        per[z.zone_id] = (round(g, 3), round(k, 3))
        if converged:
            g_sum += g
            k_sum += k
            n += 1
    ratio = (g_sum / k_sum) if k_sum > 0 else 0.0
    return HouseLoad(
        g_house=round(g_sum, 3), k_house=round(k_sum, 3),
        load_ratio=round(ratio, 3), n_eligible=n, per_zone=per,
    )



def select_regime(
    load: HouseLoad, *,
    at_peak: bool, free_cool: bool, peak_ratio: float, medium_ratio: float,
) -> str:
    """Classify the house regime. free-cool → LOW; at_peak forces PEAK even on
    priors (outdoor-sensor based, always safe). The ratio path (MEDIUM/PEAK) only
    engages once k has converged (n_eligible > 0)."""
    if free_cool:
        return REGIME_LOW
    if at_peak:
        return REGIME_PEAK
    if load.n_eligible == 0 or load.g_house <= 0:
        return REGIME_LOW
    if load.load_ratio >= peak_ratio:
        return REGIME_PEAK
    if load.load_ratio >= medium_ratio:
        return REGIME_MEDIUM
    return REGIME_LOW



def _plan_summary(
    *,
    season: str | None,
    cooling: bool,
    heating: bool,
    free_cool: bool,
    precool: bool,
    at_peak: bool,
    in_cooloff: bool,
) -> str:
    """Collapse the regime to a single word (the sensor's state).

    Summer precedence: free_cool > pre_cool > peak_run > duty_rest > cooling >
    idle. (free-cool, pre-cool and peak are effectively mutually exclusive, but
    the order makes the dominant intent unambiguous.) Winter only reflects the
    heating call for now — the anticipatory pre-heat (#7) is not built yet.
    """
    if season != _SEASON_SUMMER:
        return "heating" if heating else "idle"
    if free_cool:
        return "free_cool"
    if precool:
        return "pre_cool"
    if at_peak:
        return "peak_run"
    if in_cooloff:
        return "duty_rest"
    if cooling:
        return "cooling"
    return "idle"



def build_plan(
    state: HouseState,
    run_plan: RunPlan,
    desired: dict[str, str | float | None],
    duty: DutyState,
    forecast: list[tuple[datetime, float]],
    lookahead: timedelta,
) -> PlanView:
    """Project one cycle's state into the dashboard PlanView (pure).

    `desired` must be the PURE policy stack's merged output (presets/setpoints/
    covers) — NOT including the stateful Duty/Fan controllers — so building the
    plan has no side effects. Duty run/rest windows come from `duty`, the live
    DutyState (only meaningful while #9 is actually running).
    """
    free_cool = _is_free_cooling(state)
    cooling = state.consenso_freddo == "on"
    heating = state.consenso_caldo == "on"
    at_peak = (
        state.outdoor_temp is not None
        and state.duty_peak_outdoor is not None
        and state.outdoor_temp >= state.duty_peak_outdoor
    )

    effective = None
    if state.house_setpoint is not None and state.mode_offset is not None:
        effective = round(state.house_setpoint + state.mode_offset, 1)
    precool_setpoint = None
    if run_plan.precool and effective is not None and state.precool_offset is not None:
        precool_setpoint = round(effective - state.precool_offset, 1)

    in_cooloff = duty.cooloff_until is not None
    stint_elapsed = (
        state.now - duty.stint_start if duty.stint_start is not None else None
    )
    if not state.duty_enabled:
        blocco_desired = None
    elif at_peak or run_plan.precool:
        blocco_desired = BLOCCO_RELEASE
    elif in_cooloff:
        blocco_desired = BLOCCO_BLOCK
    else:
        blocco_desired = BLOCCO_RELEASE

    zones = tuple(
        ZonePlan(
            zone_id=z.zone_id,
            name=z.name,
            temp=z.temp,
            target=(
                desired.get(temperature_lever(z.climate)) if z.climate else None
            ),
            demand=z.demand,
            enabled=z.enabled,
            paused=z.paused,
        )
        for z in state.zones.values()
    )
    covers_closing = tuple(
        cover.entity_id
        for cover in state.covers
        if cover_lever(cover.entity_id) in desired
    )
    window = tuple(
        (when, t)
        for (when, t) in forecast
        if state.now <= when <= state.now + lookahead and t is not None
    )

    return PlanView(
        summary=_plan_summary(
            season=state.season,
            cooling=cooling,
            heating=heating,
            free_cool=free_cool,
            precool=run_plan.precool,
            at_peak=at_peak,
            in_cooloff=in_cooloff,
        ),
        season=state.season,
        house_mode=state.house_mode,
        cooling=cooling,
        free_cool=free_cool,
        precool=run_plan.precool,
        at_peak=at_peak,
        forecast_peak=run_plan.forecast_peak,
        peak_eta=run_plan.peak_eta,
        house_setpoint=state.house_setpoint,
        effective_setpoint=effective,
        precool_setpoint=precool_setpoint,
        in_cooloff=in_cooloff,
        cooloff_until=duty.cooloff_until,
        stint_start=duty.stint_start,
        stint_elapsed=stint_elapsed,
        stint_cap=state.duty_max_stint,
        blocco=state.blocco,
        blocco_desired=blocco_desired,
        duty_enabled=state.duty_enabled,
        zones=zones,
        covers_closing=covers_closing,
        forecast=window,
    )



# --- F3b: 12h per-room forward simulation + precool scheduler (pure) ----------
# Integrate each room's grey-box forward on the forecast under the F1 band/fan
# policy, to (a) show the 12h intent per room and (b) size pre-cool. Reuses
# band_step / cooling_load / capacity_fan — never re-derives them. Plan-only:
# nothing here actuates. NOTE: until k is learned (F2), hard-room trajectories are
# ADVISORY — the 4-param model can't reproduce the verified ~0-net cooling at the
# 34°C peak; comfort is guaranteed by the live band, never by this prediction.


@dataclass(frozen=True)
class RoomParams:
    """Thermal model + control params for one room's simulation.

    `pulldown_hours` / `run_floor` / `peak_outdoor` mirror the live RUN-fan
    sizing law (control_law.run_fan_pct) so the simulated fan matches what the
    band would actually command; the defaults keep older constructors valid."""

    a: float
    b: float
    c: float
    k: float
    pulldown: float
    fan_min: int
    fan_step: int = 10
    pulldown_hours: float = 2.0
    run_floor: int = 0
    peak_outdoor: float | None = None



@dataclass(frozen=True)
class TrajPoint:
    minute: int          # offset from now
    temp: float          # predicted room temp
    setpoint: float      # band setpoint commanded at this step
    fan: int             # quantized fan %
    phase: str           # run / rest
    saturated: bool      # fan demand >= 100% (can't cool faster)



@dataclass(frozen=True)
class RoomTrajectory:
    zone_id: str
    points: tuple[TrajPoint, ...] = ()
    precool_depth: float = 0.0
    precool_start_min: int | None = None
    peak_breach: bool = False     # comfort upper exceeded somewhere in the horizon
    max_temp: float | None = None



def _forecast_temp_at(
    forecast: list[tuple[datetime, float]], when: datetime
) -> float | None:
    """Step-interpolate the forecast: the last point at/before `when` (else the
    first available)."""
    best = None
    for w, t in forecast:
        if t is None:
            continue
        if w <= when:
            best = t
        else:
            return best if best is not None else t
    return best



def _solar_at(solar: list[float] | None, i: int) -> float:
    if not solar:
        return 0.0
    return solar[min(i, len(solar) - 1)]



def simulate_room(
    *, zone_id: str, params: RoomParams, t0: float,
    center: float, band: float, slam: float,
    forecast: list[tuple[datetime, float]], solar: list[float] | None,
    now: datetime, lookahead: timedelta,
    water_available: list[bool] | None = None,
    precool_depth: float = 0.0, precool_start_min: int | None = None,
    dt_min: int = 15,
) -> RoomTrajectory:
    """Forward-Euler the room under the band/fan policy. Pre-cool lowers the band
    CENTER by `precool_depth` from `precool_start_min` up to the forecast peak.
    Euler is sub-stepped so a large learned k can't make the trajectory explode."""
    n_steps = max(1, int(lookahead.total_seconds() / 60 // dt_min))
    dt_h = dt_min / 60.0
    n_sub = max(1, math.ceil((params.a + params.k) * dt_h / 0.25))
    sub_h = dt_h / n_sub
    half = band / 2.0
    pk = peak_window(forecast, now, lookahead)
    peak_min = int((pk[0] - now).total_seconds() / 60) if pk else None

    temp = t0
    phase = "run" if t0 >= center else "rest"
    points: list[TrajPoint] = []
    breach = False
    max_temp = t0
    for i in range(n_steps + 1):
        minute = i * dt_min
        when = now + timedelta(minutes=minute)
        t_out = _forecast_temp_at(forecast, when)
        if t_out is None:
            t_out = temp  # no forecast -> assume neutral envelope
        s = _solar_at(solar, i)
        eff_center = center
        if (
            precool_depth > 0 and precool_start_min is not None
            and minute >= precool_start_min
            and (peak_min is None or minute <= peak_min)
        ):
            eff_center = center - precool_depth
        phase, setpoint = band_step(
            phase, eligible=True, temp=temp, center=eff_center, band=band, slam=slam
        )
        if phase == "run":
            load = cooling_load(temp, t_out, s, a=params.a, b=params.b, c=params.c)
            pull = effective_pulldown(
                temp, eff_center, base=params.pulldown, hours=params.pulldown_hours
            )
            u_needed = (load + pull) / params.k if params.k > 0 else 1.0
            fan = run_fan_pct(
                temp=temp, outdoor=t_out, solar=s, center=eff_center, band=band,
                a=params.a, b=params.b, c=params.c, k=params.k,
                pulldown=params.pulldown, pulldown_hours=params.pulldown_hours,
                run_floor=params.run_floor, fan_min_pct=params.fan_min,
                at_peak=(
                    params.peak_outdoor is not None and t_out is not None
                    and t_out >= params.peak_outdoor
                ),
                step=params.fan_step,
            )
        else:
            u_needed = 0.0
            fan = params.fan_min
        points.append(TrajPoint(
            minute=minute, temp=round(temp, 2),
            setpoint=round(setpoint if setpoint is not None else eff_center, 2),
            fan=fan, phase=phase, saturated=u_needed >= 1.0,
        ))
        max_temp = max(max_temp, temp)
        if temp > center + half + 1e-6:
            breach = True
        # integrate to the next step (sub-stepped Euler).
        w = True if water_available is None else water_available[min(i, len(water_available) - 1)]
        u = fan / 100.0
        for _ in range(n_sub):
            d = params.a * (t_out - temp) + params.b * s + params.c
            if w:
                d -= params.k * u
            temp += d * sub_h
    return RoomTrajectory(
        zone_id=zone_id, points=tuple(points),
        precool_depth=precool_depth, precool_start_min=precool_start_min,
        peak_breach=breach, max_temp=round(max_temp, 2),
    )



def schedule_precool(
    *, zone_id: str, params: RoomParams, t0: float,
    center: float, band: float, slam: float,
    forecast: list[tuple[datetime, float]], solar: list[float] | None,
    now: datetime, lookahead: timedelta,
    water_available: list[bool] | None = None,
    max_depth: float = 3.0, grid_points: int = 12, dt_min: int = 15,
) -> RoomTrajectory:
    """Pick the SMALLEST pre-cool depth that keeps the room in comfort through the
    peak, via a fixed-start depth GRID SCAN (bisection is unsound here: feasibility
    isn't monotone in start time). No breach with depth 0 → no pre-cool. If even
    max_depth still breaches (gain-limited) → return it flagged peak_breach."""
    base = simulate_room(
        zone_id=zone_id, params=params, t0=t0, center=center, band=band, slam=slam,
        forecast=forecast, solar=solar, now=now, lookahead=lookahead,
        water_available=water_available, dt_min=dt_min,
    )
    if not base.peak_breach:
        return base
    pk = peak_window(forecast, now, lookahead)
    if pk is None:
        return base
    peak_min = int((pk[0] - now).total_seconds() / 60)
    g_peak = cooling_load(
        center, pk[1], _solar_at(solar, peak_min // dt_min),
        a=params.a, b=params.b, c=params.c,
    )
    worst = base
    for j in range(1, grid_points + 1):
        depth = max_depth * j / grid_points
        lead = math.ceil(depth / max(0.1, params.k - g_peak) * 60.0)
        start = max(0, peak_min - lead)
        traj = simulate_room(
            zone_id=zone_id, params=params, t0=t0, center=center, band=band,
            slam=slam, forecast=forecast, solar=solar, now=now, lookahead=lookahead,
            water_available=water_available, precool_depth=depth,
            precool_start_min=start, dt_min=dt_min,
        )
        if not traj.peak_breach:
            return traj   # smallest feasible depth
        worst = traj
    return worst          # max depth still breaches -> advisory peak_breach



def _downsample(traj: RoomTrajectory, downsample_min: int, dt_min: int) -> RoomTrajectory:
    """Thin a trajectory to ~hourly points to keep the sensor attribute small."""
    stride = max(1, downsample_min // dt_min)
    pts = traj.points[::stride]
    if traj.points and pts and pts[-1].minute != traj.points[-1].minute:
        pts = (*pts, traj.points[-1])
    return replace(traj, points=tuple(pts))



def build_room_plans(
    state: HouseState, params_by_zone: dict[str, RoomParams],
    forecast: list[tuple[datetime, float]], solar: list[float] | None,
    lookahead: timedelta, *,
    dt_min: int = 15, downsample_min: int = 60, max_precool_depth: float = 3.0,
    solar_by_zone: dict[str, list[float]] | None = None,
) -> tuple[RoomTrajectory, ...]:
    """Per-leader 12h trajectory + pre-cool schedule (downsampled). Plan-only.

    `solar_by_zone` (STORY_SEFF §6 row 7): per-zone S_eff curves; a zone absent
    from the dict sims on the house `solar` curve (the GHI-identity zones)."""
    if state.house_setpoint is None or state.mode_offset is None:
        return ()
    center = state.house_setpoint + state.mode_offset
    band = state.band_width if state.band_width is not None else 1.5
    slam = state.band_slam if state.band_slam is not None else 0.75
    free = _is_free_cooling(state)
    n_steps = max(1, int(lookahead.total_seconds() / 60 // dt_min)) + 1
    out: list[RoomTrajectory] = []
    for z in state.zones.values():
        if not _is_cooling_leader(z) or not z.enabled or z.paused:
            continue
        if z.bedroom and state.night_active:
            continue
        if z.temp is None or z.zone_id not in params_by_zone:
            continue
        wa = [False] * n_steps if free else None
        traj = schedule_precool(
            zone_id=z.zone_id, params=params_by_zone[z.zone_id], t0=z.temp,
            center=center + z.comfort_relax, band=band, slam=slam,
            forecast=forecast,
            solar=(solar_by_zone or {}).get(z.zone_id, solar),
            now=state.now, lookahead=lookahead, water_available=wa,
            max_depth=max_precool_depth, dt_min=dt_min,
        )
        out.append(_downsample(traj, downsample_min, dt_min))
    return tuple(out)



# --- F4a: solar forecast (pure) ----------------------------------------------
# gw3000a reads only the CURRENT horizontal irradiance; the 12h sim needs a curve.
# Estimate it from sun elevation + forecast cloud cover. Output is W/m² on the
# HORIZONTAL plane to MATCH the gw3000a pyranometer that the model's b was fit
# against — a plane-of-array unit mismatch would silently rescale b.


def clear_sky_solar(
    *, elevation_deg: float, clear_sky_ghi: float, cloud_fraction: float | None
) -> float:
    """Horizontal GHI proxy (W/m²): clear_sky_ghi · sin(elevation) · (1 − cloud).
    Below the horizon → 0; missing cloud → assume clear."""
    if elevation_deg <= 0:
        return 0.0
    cloud = 0.0 if cloud_fraction is None else max(0.0, min(1.0, cloud_fraction))
    return clear_sky_ghi * math.sin(math.radians(elevation_deg)) * (1.0 - cloud)



def solar_forecast_curve(
    *, elevations: list[float], clouds: list[float | None], clear_sky_ghi: float
) -> list[float]:
    """Per-step horizontal-GHI estimate from the sun-elevation track + the
    per-step cloud fraction. Guards missing cloud (assume clear)."""
    out: list[float] = []
    for i, elev in enumerate(elevations):
        cloud = clouds[i] if i < len(clouds) else None
        out.append(round(
            clear_sky_solar(
                elevation_deg=elev, clear_sky_ghi=clear_sky_ghi, cloud_fraction=cloud
            ), 1,
        ))
    return out



# --- F4a-v2: nowcast-anchored solar curve (pure) -----------------------------
# The regional weather cloud is unreliable here (Met.no said "rainy" while the
# gw3000a pyranometer read 1044 W/m²; Forecast.Solar under-called the sunniest
# day). Fix: pin the clear-sky×cloud curve to the LIVE gw3000a reading at step 0
# and propagate that bias forward. Note the clear_sky_ghi constant cancels in the
# ratio (curve[i] = actual_now · shape[i]/shape[0]), so the curve self-calibrates
# to reality — the forecast only has to get the relative SHAPE (sun track × cloud)
# roughly right, not the absolute level.


def solar_nowcast_bias(
    actual_now: float | None, model_now: float, *,
    lo: float = 0.4, hi: float = 2.5, min_model: float = 30.0,
) -> float:
    """Bias factor pinning a clear-sky×cloud curve to the live pyranometer NOW.

    Returns actual_now / model_now, clamped to [lo, hi]. 1.0 (no correction) when
    the sun isn't meaningfully up (model below `min_model`) or no live reading —
    so a dark/near-horizon step can't produce a wild ratio.
    """
    if actual_now is None or model_now < min_model:
        return 1.0
    return max(lo, min(hi, actual_now / model_now))



def solar_curve_v2(
    *, elevations: list[float], clouds: list[float | None], clear_sky_ghi: float,
    actual_now: float | None = None,
) -> tuple[list[float], bool]:
    """Nowcast-anchored horizontal-GHI curve. Returns (curve, anchored).

    The clear-sky×cloud shape from `solar_forecast_curve`, scaled by
    `solar_nowcast_bias` so step 0 matches the live gw3000a reading. Falls back to
    the plain curve (anchored=False) when there's no usable live reading.
    """
    base = solar_forecast_curve(
        elevations=elevations, clouds=clouds, clear_sky_ghi=clear_sky_ghi
    )
    if not base:
        return base, False
    bias = solar_nowcast_bias(actual_now, base[0])
    if bias == 1.0:
        return base, False
    return [round(v * bias, 1) for v in base], True


# --- F4c: unified band-center REFERENCE schedule (Track B, pure) --------------
# ONE joint 12h scheduler for the per-leader band CENTER, composing the shipping
# pure cores — schedule_precool (#9), energy_precool_decision (PV bank/coast),
# run_rest_durations (duty intent), return_lead_time (#8) — instead of the myopic
# fixed-priority ladder. It emits a REFERENCE ONLY: the reactive band (band_step +
# duty_comfort_max ceiling + comfort-breach-forces-RUN) still owns the model-free
# comfort guarantee and CLAMPS this reference into [comfort_floor, duty_comfort_max].
# PLAN-ONLY here (Phase 5): nothing consumes it for control yet (Phase 6, gated).


@dataclass(frozen=True)
class CenterPoint:
    """One step of a zone's reference center trajectory."""

    minute: int          # offset from the schedule's created_at
    center: float        # reference band center at this step
    source: str = "base"  # dominant driver: base | pv_bank | pv_coast | precool


@dataclass(frozen=True)
class ZoneCenterSchedule:
    """A leader zone's reference center trajectory over the horizon."""

    zone_id: str
    points: tuple[CenterPoint, ...] = ()
    precool_depth: float = 0.0
    precool_start_min: int | None = None
    # D1: may this reference DRIVE the center (Phase 6)? Else it stays ADVISORY —
    # a hard gain-limited room whose k hasn't converged (comfort held by the band).
    eligible: bool = False


@dataclass(frozen=True)
class CenterSchedule:
    """The unified 12h band-center reference + an advisory house intent.

    `.at(zone, now)` looks up the reference center; `is_stale` gates Phase-6 use on
    the age of the last good refresh (a stale 12h reference is confidently wrong —
    sized to a peak that has moved — so Phase 6 falls back to the base center, never
    to a stale reference)."""

    zones: dict = field(default_factory=dict)   # zone_id -> ZoneCenterSchedule
    created_at: datetime | None = None           # validity/staleness stamp
    horizon: timedelta | None = None
    dt_min: int = 15
    house_blocco: str | None = None       # advisory: the reference never BLOCKs
    house_run: timedelta | None = None    # advisory duty run length
    house_rest: timedelta | None = None   # advisory duty rest length
    return_lead: timedelta | None = None  # advisory #8 lead time (ETA armed)
    # STORY_SEFF §1.4: which solar domain the schedule was simulated in — "seff"
    # (per-zone facade curves) or "ghi" (house curve). A plan/actuation domain
    # divergence must be visible in diagnostics, never silent.
    solar_domain: str = "ghi"

    def at(self, zone_id: str, now: datetime) -> float | None:
        """Step-interpolated reference center for a zone at `now` (None if absent)."""
        zs = self.zones.get(zone_id)
        if zs is None or self.created_at is None or not zs.points:
            return None
        minute = (now - self.created_at).total_seconds() / 60.0
        best = zs.points[0].center
        for p in zs.points:
            if p.minute <= minute:
                best = p.center
            else:
                break
        return best

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        return self.created_at is None or (now - self.created_at) > max_age


def planner_ref(
    schedule: "CenterSchedule | None", *,
    zone_id: str, now: datetime,
    planner_eligible: bool, unified_enabled: bool,
    center_base: float, comfort_floor: float | None, comfort_ceiling: float | None,
    max_age: timedelta,
) -> float | None:
    """F4c Phase 6: the clamped planner REFERENCE center for a zone, or None to use
    the fallback ladder. Returns a value ONLY when every gate passes:
      * the unified planner is enabled (switch on),
      * the zone is planner-eligible (D1: k converged + solar-excited),
      * a schedule exists and is not stale (age <= max_age; else the reference is
        confidently wrong — fall back to base),
      * the mode is in the comfort band (center_base <= ceiling; a Via/Notte deep
        setback stays on the reactive ladder, which handles setback correctly),
      * the schedule has a point for this zone.
    The reference is clamped into [comfort_floor, comfort_ceiling] — the reactive
    band's own comfort bounds — so the model can only ever cost efficiency, never
    comfort. All gates false-safe to None (the ladder)."""
    if not unified_enabled or schedule is None or not planner_eligible:
        return None
    if comfort_ceiling is not None and center_base > comfort_ceiling:
        return None  # deep setback -> the ladder owns the center
    if schedule.is_stale(now, max_age):
        return None
    ref = schedule.at(zone_id, now)
    if ref is None:
        return None
    if comfort_floor is not None:
        ref = max(comfort_floor, ref)
    if comfort_ceiling is not None:
        ref = min(ref, comfort_ceiling)
    return ref


# --- R1 (Tier-1): the ONE resolved band center --------------------------------
# resolve_center is the SINGLE site composing a leader's band center — planner
# reference ▸ compose_center ladder ▸ base — and annotate_centers writes it onto
# each eligible leader's ZoneSnapshot once per cycle, so every consumer (the
# FanBandController slam, the coalescing coordinator, sensor.hvac_plan) reads the
# SAME center. This kills the base-vs-shifted mismatch (the coordinator deciding
# RUN/REST off the base center while the band slams the PV/planner-shifted one)
# that blocked the pv_bias × regime co-enable.


@dataclass(frozen=True)
class CenterResolution:
    """How one leader's band center resolved this cycle."""

    center: float          # the resolved center every consumer reads
    base: float            # house_setpoint + mode_offset (the mode's center)
    source: str            # planner | base | pv_bank | pv_coast | precool | comfort_relax
    floored: bool          # the LADDER's comfort floor clamped a lowering feature
    planner_driven: bool   # the unified planner reference drove the center


def resolve_center(
    zone: ZoneSnapshot, state: HouseState, *, max_age: timedelta
) -> CenterResolution | None:
    """Resolve a leader's band center. Precedence: `planner_ref` (already clamped
    into [comfort_floor, duty_comfort_max] — the one clamp site) when the unified
    planner drives, else the reactive `compose_center` ladder (base + at most one
    feature, bounded by the floor/ceiling per its own contract). A verbatim
    relocation of the wiring formerly inline in FanBandController and duplicated
    in engine._center_compositions. None when there is no base center.

    `floored` is always the LADDER's flag (what the comfort floor did to the
    composed center), even when the planner drives — matching the pre-R1
    sensor semantics; the planner reference carries its own clamp."""
    if state.house_setpoint is None or state.mode_offset is None:
        return None
    # #2: per-zone comfort trim stacks on the house base (mode offset included).
    base = state.house_setpoint + state.mode_offset + zone.setpoint_offset
    comp = compose_center(
        base=base,
        pv_mode=state.pv_mode, pv_floor=state.pv_floor,
        pv_coast_relax=state.pv_coast_relax,
        comfort_enabled=state.comfort_enabled, comfort_relax=zone.comfort_relax,
        precool=state.precool, precool_offset=state.precool_offset,
        duty_enabled=state.duty_enabled,
        comfort_ceiling=state.duty_comfort_max,
        comfort_floor=state.comfort_floor,
    )
    ref = planner_ref(
        state.center_schedule, zone_id=zone.zone_id, now=state.now,
        planner_eligible=zone.model_planner_eligible,
        unified_enabled=state.unified_planner_enabled,
        center_base=base, comfort_floor=state.comfort_floor,
        comfort_ceiling=state.duty_comfort_max, max_age=max_age,
    )
    if ref is not None:
        return CenterResolution(
            center=ref, base=base, source="planner",
            floored=comp.floored, planner_driven=True,
        )
    return CenterResolution(
        center=comp.center, base=base, source=comp.source,
        floored=comp.floored, planner_driven=False,
    )


def annotate_centers(state: HouseState, *, max_age: timedelta) -> HouseState:
    """Write the resolved center onto every eligible cooling leader's snapshot.

    Eligibility mirrors the FanBandController's: a cooling leader, enabled, not
    paused, not free-cooling, not a bedroom owned by camere silenziose (#2b), with
    a base center. Ineligible zones keep the ZoneSnapshot defaults
    (None/"none"/False/False). Called by the engine ONCE per cycle, AFTER the #8
    effective-mode override, _pv_bias_apply and the schedule attach — they feed
    the resolution (see the ordering comment in engine._cycle)."""
    if state.house_setpoint is None or state.mode_offset is None:
        return state
    free = _is_free_cooling(state)
    zones = dict(state.zones)
    changed = False
    for zid, z in state.zones.items():
        if not _is_cooling_leader(z) or not z.enabled or z.paused or free:
            continue
        if z.bedroom and state.night_active:
            continue
        res = resolve_center(z, state, max_age=max_age)
        if res is None:
            continue
        zones[zid] = replace(
            z, resolved_center=res.center, center_source=res.source,
            center_floored=res.floored, planner_driven=res.planner_driven,
        )
        changed = True
    return replace(state, zones=zones) if changed else state


def plan_center_schedule(
    measured: HouseState,
    params_by_zone: dict,
    forecast: list[tuple[datetime, float]],
    solar: list[float] | None,
    *,
    lookahead: timedelta,
    max_precool_depth: float,
    pv_active: bool = False,
    pv_kwh_remaining: float | None = None,
    consumption_kwh_remaining: float | None = None,
    pv_floor_rich: float = 22.0,
    pv_floor_poor: float = 23.0,
    pv_coast_relax: float = 0.0,
    pv_eff_fraction: float = 0.6,
    pv_eff_min: float = 0.1,
    eta: datetime | None = None,
    return_max_lead: timedelta = timedelta(hours=6),
    return_margin: timedelta = timedelta(minutes=30),
    dt_min: int = 15,
    solar_by_zone: dict[str, list[float]] | None = None,
) -> CenterSchedule:
    """Jointly schedule a per-leader band-center REFERENCE over the lookahead.

    Composition (each hour, deepest LOWERING wins, RAISING capped at the ceiling,
    all bounded below by the comfort floor):
      base = house_setpoint + mode_offset
      · PV bank -> center toward pv_floor / PV coast -> center + relax (capped)
        [energy_precool_decision, looking forward from each hour]
      · #9 pre-cool -> center - depth between start and the forecast peak
        [schedule_precool]
    Plus an advisory house intent: duty run/rest [run_rest_durations] and the #8
    arrival lead time [return_lead_time]. PLAN-ONLY — emits a reference, drives
    nothing; the reactive band clamps + owns comfort.
    """
    now = measured.now
    if measured.house_setpoint is None or measured.mode_offset is None:
        return CenterSchedule(
            created_at=now, horizon=lookahead, dt_min=dt_min,
            house_blocco=BLOCCO_RELEASE,
        )
    base = measured.house_setpoint + measured.mode_offset
    band = measured.band_width if measured.band_width is not None else 1.5
    slam = measured.band_slam if measured.band_slam is not None else 0.75
    floor = measured.comfort_floor
    ceiling = measured.duty_comfort_max
    free = _is_free_cooling(measured)
    n_steps = max(1, int(lookahead.total_seconds() / 60 // dt_min))
    pk = peak_window(forecast, now, lookahead)
    peak_min = int((pk[0] - now).total_seconds() / 60) if pk else None

    zones_out: dict[str, ZoneCenterSchedule] = {}
    for z in measured.zones.values():
        if not _is_cooling_leader(z) or not z.enabled or z.paused or free:
            continue
        if z.bedroom and measured.night_active:
            continue
        if z.temp is None or z.zone_id not in params_by_zone:
            continue
        params = params_by_zone[z.zone_id]
        # STORY_SEFF §6 row 8: this zone's own S_eff curve (house curve for
        # GHI-identity zones) — an S_eff-fitted b × the house GHI would mix units.
        zone_solar = (solar_by_zone or {}).get(z.zone_id, solar)
        # #9 pre-cool depth + start (reuse the shipping scheduler).
        traj = schedule_precool(
            zone_id=z.zone_id, params=params, t0=z.temp, center=base, band=band,
            slam=slam, forecast=forecast, solar=zone_solar, now=now,
            lookahead=lookahead, max_depth=max_precool_depth, dt_min=dt_min,
        )
        depth, start_min = traj.precool_depth, traj.precool_start_min
        # Effectiveness horizon at the base center (for the PV ranking).
        eff: list[float] = []
        for h in range(n_steps + 1):
            when = now + timedelta(minutes=h * dt_min)
            t_out = _forecast_temp_at(forecast, when)
            if t_out is None:
                t_out = measured.outdoor_temp
            eff.append(cooling_effectiveness(
                base, t_out, _solar_at(zone_solar, h),
                a=params.a, b=params.b, c=params.c, k=params.k,
            ))
        points: list[CenterPoint] = []
        for h in range(n_steps + 1):
            minute = h * dt_min
            center_h = base
            src = "base"
            if pv_active:
                d = energy_precool_decision(
                    effectiveness=eff, now_index=h,
                    pv_kwh_remaining=pv_kwh_remaining,
                    consumption_kwh_remaining=consumption_kwh_remaining,
                    eff_fraction=pv_eff_fraction, eff_min=pv_eff_min,
                    floor_rich=pv_floor_rich, floor_poor=pv_floor_poor,
                )
                if d.mode == PRECOOL_BANK and d.floor is not None:
                    center_h = min(center_h, d.floor)
                    src = "pv_bank"
                elif d.mode == PRECOOL_COAST:
                    raised = base + pv_coast_relax
                    if ceiling is not None:
                        raised = min(raised, ceiling)
                    center_h = raised
                    src = "pv_coast"
            # #9 pre-cool overlay: deepest lowering wins, between start and the peak.
            if depth > 0 and start_min is not None and minute >= start_min and (
                peak_min is None or minute <= peak_min
            ):
                pc = base - depth
                if pc < center_h:
                    center_h = pc
                    src = "precool"
            # comfort FLOOR bounds all lowering (symmetric to the ceiling on raising).
            if floor is not None and center_h < floor:
                center_h = floor
            points.append(
                CenterPoint(minute=minute, center=round(center_h, 2), source=src)
            )
        zones_out[z.zone_id] = ZoneCenterSchedule(
            zone_id=z.zone_id, points=tuple(points),
            precool_depth=depth, precool_start_min=start_min,
            eligible=bool(z.model_planner_eligible),
        )

    # Advisory house duty intent: run/rest length from the aggregate load.
    g_sum = k_sum = 0.0
    for zid, p in params_by_zone.items():
        zz = measured.zones.get(zid)
        if zz is not None and zz.temp is not None and zid in zones_out:
            g_sum += max(0.0, cooling_load(
                zz.temp, measured.outdoor_temp, zz.s_eff, a=p.a, b=p.b, c=p.c
            ))
            k_sum += p.k
    run, rest = (
        run_rest_durations(g_sum, k_sum, 1.0, band) if k_sum > 0 else (None, None)
    )
    # Advisory #8 arrival lead time (only when an ETA is armed).
    lead = None
    if eta is not None:
        rooms = [
            ReturnRoom(
                temp=zz.temp, target=measured.house_setpoint,
                a=p.a, b=p.b, c=p.c, k=p.k, s_eff=zz.s_eff,
            )
            for zid, p in params_by_zone.items()
            if (zz := measured.zones.get(zid)) is not None and zz.temp is not None
        ]
        if rooms:
            lead = return_lead_time(
                rooms, measured.outdoor_temp, measured.solar,
                max_lead=return_max_lead, margin=return_margin,
            )
    return CenterSchedule(
        zones=zones_out, created_at=now, horizon=lookahead, dt_min=dt_min,
        house_blocco=BLOCCO_RELEASE,   # the reference never BLOCKs; reactive owns it
        house_run=run, house_rest=rest, return_lead=lead,
        solar_domain="seff" if solar_by_zone else "ghi",
    )
