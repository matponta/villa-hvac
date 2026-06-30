"""Supervisor core — the write-arbiter that runs the house as one organism.

Phase A. Today's controllers (#2/#4/#2b/#2c) each call `climate.*` directly and
race over the same levers. The target is a single Supervisor that, each cycle,
builds one house-state model, runs a priority-ordered policy stack to compute the
*desired* state of every lever, and writes each lever once, idempotently.

This module is the heart of that: the **reconcile state machine** + the priority
**merge** of policy outputs. It is intentionally PURE (no Home Assistant imports)
so the control discipline is fully unit-testable — especially the
manual-override detection, the #1 robustness risk on a flaky KNX bus:

    A single `current != last-written` read is ambiguous. KNX drops telegrams
    (the salotto write loss) and lags attributes (AUTO fan % bounces in
    sub-second triplets), which look identical to a hand change. So we never
    declare "manual" on one read: after writing X we expect X within tolerance;
    if it diverges we RE-ASSERT for N cycles; only divergence that survives the
    re-asserts concedes to manual (back off for a while). A dropped telegram
    converges on re-assert and never trips the override.

The HA wiring (state-model builder, service calls, enable switches, fail-safe)
lives in engine.py; nothing here imports homeassistant.

Deploy-dark (decided 2026-06-27): the master `switch.supervisor` gates the
ENTIRE engine — including the migrated #2/#4/#10 — not just the new optimization
layer. On deploy nothing actuates until the switch is flipped, then the whole
organism lights up at once. Every controller path therefore routes through the
engine and is a no-op while the master is off.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math

# --- Tunable defaults (move to the options flow later) -----------------------
# |current - desired| <= this counts as "matched" (°C, for setpoints). Presets
# and switch states compare by string equality regardless of this.
DEFAULT_SETPOINT_TOLERANCE = 0.3
# Re-write a diverged lever for this many cycles before conceding to manual.
DEFAULT_MAX_REASSERTS = 3
# Once conceded, leave the lever alone (manual wins) for this long.
DEFAULT_OVERRIDE_BACKOFF = timedelta(hours=2)

# State strings that mean "don't conclude anything this cycle".
TRANSIENT_STATES: tuple[str | None, ...] = ("unavailable", "unknown", None, "")

# Consenso BLOCCO switch states (verify polarity live before actuating; observed
# 2026-06-27: OFF = released/cooling allowed).
BLOCCO_BLOCK = "on"      # block the villa cooling call to the PdC
BLOCCO_RELEASE = "off"   # allow the villa to cool


@dataclass(frozen=True)
class LeverState:
    """Per-lever bookkeeping for the reconcile state machine.

    `written` is the value we last asserted; `attempts` counts consecutive
    re-asserts while the read stays diverged; `override_until` is set when we
    have conceded the lever to a manual change.
    """

    written: str | None = None
    attempts: int = 0
    override_until: datetime | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of one reconcile: the new lever state + an optional write.

    `write` is the value to send this cycle (None = do nothing). `note` is a
    diagnostic label for logging/tests, never a control input.
    """

    state: LeverState
    write: str | None = None
    note: str = ""


def values_match(
    current: str | float | None,
    desired: str | float | None,
    tolerance: float,
) -> bool:
    """True if a read equals a target. Numbers compare within `tolerance`
    (setpoints); everything else compares as strings (presets, on/off)."""
    if current is None or desired is None:
        return current == desired
    try:
        return abs(float(current) - float(desired)) <= tolerance
    except (TypeError, ValueError):
        return str(current) == str(desired)


def reconcile(
    desired: str | float | None,
    current: str | float | None,
    state: LeverState,
    now: datetime,
    *,
    tolerance: float = DEFAULT_SETPOINT_TOLERANCE,
    max_reasserts: int = DEFAULT_MAX_REASSERTS,
    backoff: timedelta = DEFAULT_OVERRIDE_BACKOFF,
) -> ReconcileResult:
    """Decide what (if anything) to write for one lever this cycle.

    `desired` is the merged policy opinion (None = no opinion → release control).
    `current` is the live read (may be a transient `unavailable`/`unknown`).
    Returns the next `LeverState` and an optional write. Pure: same inputs →
    same output, so the whole discipline is unit-testable.
    """
    # 1. Honor an active manual-override backoff: hands off the lever.
    if state.override_until is not None and now < state.override_until:
        return ReconcileResult(state=state, note="manual-hold")
    # Backoff expired → forget history and reconcile fresh.
    if state.override_until is not None:
        state = LeverState()

    # 2. No opinion → release: write nothing, drop any tracking.
    if desired is None:
        return ReconcileResult(state=LeverState(), note="released")

    # 3. Transient read → wait; never conclude "manual" from unavailable/unknown.
    if current is None or (isinstance(current, str) and current in TRANSIENT_STATES):
        return ReconcileResult(state=state, note="transient")

    # 4. Already where we want it (set by us OR by anyone) → satisfied.
    if values_match(current, desired, tolerance):
        return ReconcileResult(
            state=replace(state, written=str(desired), attempts=0), note="satisfied"
        )

    # 5. Diverged. First time we want this value (or it changed) → write it.
    if state.written is None or not values_match(state.written, desired, tolerance):
        return ReconcileResult(
            state=replace(state, written=str(desired), attempts=1),
            write=str(desired),
            note="write",
        )

    # 6. We already asserted `desired` but the read still diverges → dropped
    #    telegram or a hand change. Re-assert up to the limit before judging.
    if state.attempts < max_reasserts:
        return ReconcileResult(
            state=replace(state, attempts=state.attempts + 1),
            write=str(desired),
            note="reassert",
        )

    # 7. Divergence survived every re-assert → treat as a manual change and
    #    concede the lever for the backoff window.
    return ReconcileResult(
        state=LeverState(override_until=now + backoff), note="override"
    )


def merge_desired(
    ordered_outputs: list[dict[str, str | float | None]],
) -> dict[str, str | float | None]:
    """Merge per-policy desired-lever maps; highest priority wins per lever.

    `ordered_outputs` is the policy stack's outputs in HIGH→LOW priority order.
    The first policy to express an opinion on a lever owns it; lower policies
    cannot override. A present key with value None is an explicit "release"
    opinion and still wins over lower policies (e.g. a guardrail freeing a
    lever beats an optimizer wanting to drive it).
    """
    merged: dict[str, str | float | None] = {}
    for output in ordered_outputs:
        for lever, value in output.items():
            if lever not in merged:
                merged[lever] = value
    return merged


# --- #9 central duty-cycle (pure) --------------------------------------------
# Cap the villa's continuous cooling stint; when it's exceeded, force a cooloff
# via the Consenso BLOCCO, then release. This also SYNCHRONIZES the rooms — the
# whole villa runs together during the stint and rests together during cooloff.


@dataclass(frozen=True)
class DutyState:
    """Duty-cycle bookkeeping across cycles."""

    stint_start: datetime | None = None   # when the current cooling stint began
    cooloff_until: datetime | None = None  # block until this time


def duty_decision(
    cooling_active: bool,
    comfort_breach: bool,
    now: datetime,
    state: DutyState,
    max_stint: timedelta,
    cooloff: timedelta,
    at_peak: bool = False,
    precool: bool = False,
) -> tuple[DutyState, str]:
    """Advance the duty cycle. Returns (new_state, desired BLOCCO value).

    - Duty-adaptive: at peak (hot outside) DON'T coalesce — let the PdC run and
      lean on load reduction (#6/#7); the gain-limited rooms can't afford a rest.
    - Pre-cool (forecast feed-forward): a hot peak is coming soon -> don't rest,
      keep banking coolth.
    - In a cooloff: keep blocking until it elapses (or a comfort breach aborts it).
    - Not cooling, or a room too hot: never block; reset the stint.
    - Cooling + comfortable: accumulate the stint; once it exceeds `max_stint`,
      begin a `cooloff` (block). Comfort always wins over the timer.
    """
    if at_peak or precool:
        return DutyState(), BLOCCO_RELEASE
    if state.cooloff_until is not None:
        if comfort_breach or now >= state.cooloff_until:
            return DutyState(), BLOCCO_RELEASE
        return state, BLOCCO_BLOCK
    if not cooling_active or comfort_breach:
        return DutyState(), BLOCCO_RELEASE
    start = state.stint_start or now
    if now - start >= max_stint:
        return DutyState(cooloff_until=now + cooloff), BLOCCO_BLOCK
    return DutyState(stint_start=start), BLOCCO_RELEASE


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
    window = [
        (when, t)
        for (when, t) in forecast
        if now <= when <= now + lookahead and t is not None
    ]
    if not window:
        return RunPlan()
    peak_when, peak_temp = max(window, key=lambda wt: wt[1])
    eta = peak_when - now
    if peak_temp < peak_threshold or current_outdoor is None:
        return RunPlan(precool=False, forecast_peak=peak_temp, peak_eta=eta)
    precool = peak_when > now and (peak_temp - current_outdoor) >= margin
    return RunPlan(precool=precool, forecast_peak=peak_temp, peak_eta=eta)


# --- #3 v2: comfort-band control + capacity-matched fan (pure) ---------------
# Two-axis control of a fancoil cooling unit:
#  - SETPOINT band: we impose a wide, settable hysteresis by slamming the
#    thermostat setpoint to center-A (RUN, valve forced open) / center+A (REST,
#    valve closed), flipping at center±B/2. Long uniform cycles, no bang-bang.
#  - FAN: sized to the thermal load (capacity-matched), so it's quiet where less
#    power is needed. The band guarantees comfort regardless of fan accuracy.


@dataclass(frozen=True)
class BandState:
    """Comfort-band hysteresis phase for one cooling unit."""

    phase: str = "released"   # "released" | "run" | "rest"


def band_step(
    phase: str,
    *,
    eligible: bool,
    temp: float | None,
    center: float | None,
    band: float,
    slam: float,
) -> tuple[str, float | None]:
    """Advance the comfort-band hysteresis. Returns (new_phase, setpoint).

    RUN once temp ≥ center + B/2 (drive setpoint to center-A → valve open);
    REST once temp ≤ center - B/2 (drive setpoint to center+A → valve closed);
    within the band, hold the current phase (the wide hysteresis). setpoint is
    None when released → no opinion, the house-mode policy owns the setpoint.
    """
    if not eligible or temp is None or center is None:
        return "released", None
    half = band / 2.0
    if temp >= center + half:
        phase = "run"
    elif temp <= center - half:
        phase = "rest"
    elif phase == "released":
        phase = "run" if temp >= center else "rest"
    # else: within the band -> keep the current phase (hysteresis hold)
    if phase == "run":
        return "run", round(center - slam, 2)
    return "rest", round(center + slam, 2)


def cooling_load(
    temp: float | None,
    outdoor: float | None,
    solar: float | None,
    *,
    a: float,
    b: float,
    c: float,
) -> float:
    """Heat-gain rate G (°C/h) = a·(T_out−T) + b·S + c. Missing inputs drop their
    term. Positive G = the room is gaining heat (needs cooling to hold)."""
    g = c
    if outdoor is not None and temp is not None:
        g += a * (outdoor - temp)
    if solar is not None:
        g += b * solar
    return g


def fan_level(u: float, fan_min_pct: int, *, step: int = 10) -> int:
    """Quantize a fan effort u∈[0,1] to a level (`step`% each), floored at the
    minimum-circulation %, capped at 100."""
    pct = max(0.0, min(1.0, u)) * 100.0
    quantized = round(pct / step) * step
    return int(min(100, max(fan_min_pct, quantized)))


def capacity_fan(
    load: float,
    *,
    pulldown: float,
    capacity: float,
    fan_min_pct: int,
    step: int = 10,
    last_level: int | None = None,
    hysteresis: int = 0,
) -> int:
    """Fan % to deliver (load + pulldown) of cooling given capacity k (°C/h at
    100%). Capacity ≤ 0 → full fan (can't size). Quantized to `step` levels.

    With `last_level`/`hysteresis`, holds the previous level until the raw demand
    moves past the level boundary by more than `step/2 + hysteresis` — so the fan
    doesn't hunt between adjacent levels as the load jitters."""
    if capacity <= 0:
        return 100
    raw = max(0.0, min(1.0, (load + pulldown) / capacity)) * 100.0
    level = round(raw / step) * step
    if last_level is not None and abs(raw - last_level) < (step / 2 + hysteresis):
        level = last_level
    return int(min(100, max(fan_min_pct, level)))


# --- F2: online self-refining per-room thermal model (pure RLS) --------------
# Learn dT/dt = a(T_out−T) + b·S + c − k·u_eff per room. {a,b,c} are identified
# on w=False windows (no chilled water → the −k·u term vanishes → a clean 3-param
# regression); k is identified on w=True windows from the residual (F2b). Kept
# decoupled (separate estimators) so the two never absorb each other — the #1
# identifiability risk. Pure + bounded + NaN-rejecting so a bad sample can never
# poison the model or feed a sign-flipping k to capacity_fan.


@dataclass(frozen=True)
class ParamBounds:
    """Physical clamps for the learned params (reject anything outside)."""

    max_a: float
    max_b: float
    max_c: float
    min_k: float
    max_k: float


@dataclass(frozen=True)
class ThermalParams:
    """Per-room grey-box params + the RLS state needed to keep learning."""

    a: float
    b: float
    c: float
    k: float
    p: tuple[float, ...] = (0.0,) * 9   # 3x3 passive covariance, row-major
    p_k: float = 0.0                    # scalar k variance
    n: int = 0                          # passive ({a,b,c}) update count
    n_k: int = 0                        # capacity (k) update count


def seed_params(
    a: float, b: float, c: float, k: float, *,
    p0_passive: tuple[float, float, float], p0_k: float,
) -> ThermalParams:
    """A fresh model seeded from the priors with a weak (large) covariance."""
    pa, pb, pc = p0_passive
    return ThermalParams(
        a=a, b=b, c=c, k=k,
        p=(pa, 0.0, 0.0, 0.0, pb, 0.0, 0.0, 0.0, pc), p_k=p0_k,
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def estimate_rate(
    samples: list[tuple[datetime, float]], *, min_span_h: float
) -> float | None:
    """dT/dt in °C/h via least-squares slope over a long baseline. None if the
    span is < `min_span_h` or the data is unusable.

    Estimating over a long window (NOT a 30 s difference) is essential: the 0.1 °C
    sensor quantization over 30 s is ~12 °C/h of noise, dwarfing the ~1 °C/h
    signal — a single-step diff is pure noise.
    """
    pts = [
        (t, v) for (t, v) in samples
        if v is not None and isinstance(v, (int, float)) and math.isfinite(v)
    ]
    if len(pts) < 3:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for (t, _) in pts]
    ys = [float(v) for (_, v) in pts]
    if xs[-1] - xs[0] < min_span_h:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope if math.isfinite(slope) else None


def rls_passive_update(
    params: ThermalParams, *,
    dt_dt: float, t_out: float, temp: float, solar: float,
    forgetting: float, bounds: ParamBounds,
) -> ThermalParams:
    """One RLS step learning {a,b,c} from a w=False (no-chilled-water) window,
    where dT/dt = a(T_out−T) + b·S + c. Holds k untouched. Rejects (returns the
    prior unchanged) any non-finite or out-of-bounds update."""
    if not all(math.isfinite(v) for v in (dt_dt, t_out, temp, solar)):
        return params
    x = (t_out - temp, solar, 1.0)
    theta = (params.a, params.b, params.c)
    p = params.p
    px = (
        p[0] * x[0] + p[1] * x[1] + p[2] * x[2],
        p[3] * x[0] + p[4] * x[1] + p[5] * x[2],
        p[6] * x[0] + p[7] * x[1] + p[8] * x[2],
    )
    denom = forgetting + (x[0] * px[0] + x[1] * px[1] + x[2] * px[2])
    if not math.isfinite(denom) or denom <= 0:
        return params
    gain = (px[0] / denom, px[1] / denom, px[2] / denom)
    err = dt_dt - (x[0] * theta[0] + x[1] * theta[1] + x[2] * theta[2])
    a = _clamp(theta[0] + gain[0] * err, 0.0, bounds.max_a)
    b = _clamp(theta[1] + gain[1] * err, 0.0, bounds.max_b)
    c = _clamp(theta[2] + gain[2] * err, 0.0, bounds.max_c)
    new_p = tuple(
        (p[3 * i + j] - gain[i] * px[j]) / forgetting
        for i in range(3) for j in range(3)
    )
    if not all(math.isfinite(v) for v in (a, b, c, *new_p)):
        return params
    return replace(params, a=a, b=b, c=c, p=new_p, n=params.n + 1)


def rls_capacity_update(
    params: ThermalParams, *,
    dt_dt: float, t_out: float, temp: float, solar: float, u: float,
    forgetting: float, bounds: ParamBounds,
) -> ThermalParams:
    """One scalar-RLS step learning k from a w=True window where the fan is HELD
    at a known u∈(0,1]: dT/dt = G − k·u, so k_obs = (G − dT/dt)/u with G from the
    (frozen) passive params. Holds {a,b,c} untouched. u≤0 → no information."""
    if u is None or u <= 0 or not all(
        math.isfinite(v) for v in (dt_dt, t_out, temp, solar, u)
    ):
        return params
    g = params.a * (t_out - temp) + params.b * solar + params.c
    k_obs = (g - dt_dt) / u
    if not math.isfinite(k_obs):
        return params
    # scalar RLS on k (regressor = u): standard gain/variance recursion.
    denom = forgetting + u * params.p_k * u
    if not math.isfinite(denom) or denom <= 0:
        return params
    gain = (params.p_k * u) / denom
    # residual of the measurement model dt_dt = g - k*u  ->  (g - dt_dt) = k*u
    err = (g - dt_dt) - params.k * u
    k = _clamp(params.k + gain * err, bounds.min_k, bounds.max_k)
    new_p_k = (params.p_k - gain * u * params.p_k) / forgetting
    if not math.isfinite(k) or not math.isfinite(new_p_k):
        return params
    return replace(params, k=k, p_k=new_p_k, n_k=params.n_k + 1)


def abc_confidence(params: ThermalParams, *, conf_min: float) -> float:
    """0→1 trust in the learned {a,b,c}, crossing 0.5 at conf_min updates."""
    total = params.n + conf_min
    return params.n / total if total > 0 else 0.0


def k_confidence(params: ThermalParams, *, conf_min: float) -> float:
    """0→1 trust in the learned k, crossing 0.5 at conf_min updates."""
    total = params.n_k + conf_min
    return params.n_k / total if total > 0 else 0.0


def blend_params(
    learned: ThermalParams, prior: ThermalParams, *,
    abc_conf_min: float, k_conf_min: float,
) -> ThermalParams:
    """Hand control from the prior to the learned model as confidence grows: each
    coefficient = prior·(1−w) + learned·w, with separate weights for {a,b,c} and
    k. Below confidence the prior dominates → control behaves exactly like F1
    until a room's model has actually converged."""
    wa = abc_confidence(learned, conf_min=abc_conf_min)
    wk = k_confidence(learned, conf_min=k_conf_min)
    return replace(
        learned,
        a=prior.a * (1 - wa) + learned.a * wa,
        b=prior.b * (1 - wa) + learned.b * wa,
        c=prior.c * (1 - wa) + learned.c * wa,
        k=prior.k * (1 - wk) + learned.k * wk,
    )


# --- House-state model (pure data) -------------------------------------------
# The Supervisor builds one snapshot per cycle; policies read it and return
# desired lever settings. Keep this a plain data carrier — building it from
# Home Assistant lives in engine.py so this module stays import-pure.


@dataclass(frozen=True)
class ZoneSnapshot:
    """Per-zone slice of the house state."""

    zone_id: str
    name: str
    climate: str | None
    emitter: str | None
    temp: float | None = None      # fused current temperature (#1)
    demand: bool | None = None     # EV FAN valve open = actually cooling
    enabled: bool = True           # #10 zone enable switch
    paused: bool = False           # #4 window pause
    bedroom: bool = False          # camere silenziose zone (#2b)
    fan_min: int = 0               # rest/min-circulation fan % for this zone (#3 v2)
    fancoil: str | None = None     # primary fan entity
    manuale: str | None = None     # primary manuale switch entity
    follows: str | None = None     # leader zone_id this zone defers to (open-space)
    # All (fan, manuale) units this leader drives at one speed — e.g. living_room
    # owns both Salotto and Cucina fancoils (one open space, #3 v2).
    fancoil_units: tuple[tuple[str, str], ...] = ()
    # F2: blended (prior→learned) thermal model for this zone + confidences.
    # None until the estimator/store populates them; control falls back to priors.
    model_a: float | None = None
    model_b: float | None = None
    model_c: float | None = None
    model_k: float | None = None
    model_confidence: float | None = None    # min(abc, k) confidence, for display
    model_k_confidence: float | None = None   # k-only confidence (regime/F2b gating)
    # F2b: live actuation state, so the estimator can learn k only on held-fan
    # windows (manuale on + known %) — never from AUTO/unknown fan.
    fan_pct: int | None = None
    manuale_on: bool = False


@dataclass(frozen=True)
class CoverInfo:
    """A shadeable cover, resolved from the registries (#6).

    `target_position` is the per-room shade target (HA cover position: 0 = fully
    closed/down, 100 = open) the blind is driven to when shading triggers; None
    means "use the house default". `blocked` is the per-room manual override —
    when True the cover is skipped entirely (not closed, not reopened).
    """

    entity_id: str
    orientation: str            # north / east / south / west (device label)
    zone: str | None = None     # area_id
    floor: str | None = None    # area.floor_id
    target_position: int | None = None  # per-room shade target (HA position)
    blocked: bool = False       # per-room manual override -> skip shading


@dataclass(frozen=True)
class HouseState:
    """Unified per-cycle snapshot the policy stack reasons over."""

    now: datetime
    zones: dict[str, ZoneSnapshot] = field(default_factory=dict)
    covers: tuple[CoverInfo, ...] = ()
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    shading_enabled: bool = False
    shading_solar_threshold: float | None = None
    shading_default_position: int | None = None  # #6 fallback shade position
    band_width: float | None = None    # #3 v2 comfort band B (°C)
    band_slam: float | None = None     # #3 v2 setpoint slam A (°C)
    model_learning_enabled: bool = True  # F2 online estimator observer
    duty_enabled: bool = False          # #9 duty-cycle switch
    duty_max_stint: timedelta | None = None
    duty_cooloff: timedelta | None = None
    duty_comfort_max: float | None = None  # abort cooloff if a zone exceeds this
    duty_peak_outdoor: float | None = None  # at/above this outdoor temp -> no duty
    precool: bool = False               # #9 forecast: hot peak imminent
    precool_offset: float | None = None  # °C below target while pre-cooling
    night_active: bool = False          # #2b camere silenziose in effect
    fan_pacing_enabled: bool = False    # #3 fan pacing switch
    season: str | None = None          # summer / winter
    house_mode: str | None = None      # Casa / Via / Notte / Vacanza
    auto_setback: bool = True          # #2 global Auto setback switch
    house_setpoint: float | None = None  # dashboard slider base setpoint
    mode_offset: float | None = None   # season-aware offset for house_mode
    free_cool_enabled: bool = False    # #5 outdoor free-cooling shutoff
    free_cool_threshold: float | None = None  # outdoor below this -> suppress
    outdoor_temp: float | None = None  # Ecowitt gw3000a
    solar: float | None = None         # Ecowitt solar radiation W/m²
    consenso_freddo: str | None = None
    consenso_caldo: str | None = None
    blocco: str | None = None          # central BLOCCO switch state


# --- Lever-key helpers -------------------------------------------------------
# A lever is addressed by "<kind>:<entity>"; the engine reads/writes by kind.
# The global cooling block has no entity in its key.

BLOCCO_LEVER = "blocco"


def preset_lever(climate_entity: str) -> str:
    return f"preset:{climate_entity}"


def temperature_lever(climate_entity: str) -> str:
    return f"temperature:{climate_entity}"


def fan_lever(fan_entity: str) -> str:
    return f"fan:{fan_entity}"


def cover_lever(cover_entity: str) -> str:
    return f"cover:{cover_entity}"


def switch_lever(switch_entity: str) -> str:
    return f"switch:{switch_entity}"


# --- #11 plan view (pure) ----------------------------------------------------
# Project the organism's next-12h INTENT into a single structured view a
# dashboard can render: the forecast curve + peak, the pre-cool / peak-skip /
# duty run-rest regime, and each zone's planned setpoint. Pure so it is fully
# unit-testable and so it can be computed every cycle (read-only) even while the
# supervisor is deploy-dark — letting us watch the plan before lighting up the
# actuation. `desired` is the merged output of the PURE policy stack only (no
# stateful controllers), so computing it never advances duty/pacing timers.

# Season string (mirror of const.SEASON_SUMMER), kept local so this module
# stays import-pure (const.py imports homeassistant).
_SEASON_SUMMER = "summer"


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


def _is_free_cooling(state: HouseState) -> bool:
    return (
        state.free_cool_enabled
        and state.season == _SEASON_SUMMER
        and state.outdoor_temp is not None
        and state.free_cool_threshold is not None
        and state.outdoor_temp < state.free_cool_threshold
    )


def _is_cooling_leader(z: ZoneSnapshot) -> bool:
    """A cooling fancoil LEADER: owns a thermostat + its fancoil units and is not
    a follower (open-space followers like Cucina are driven by their leader).
    The single shared definition for FanBandController / ThermalEstimator / the
    regime index / the planner, so the set never drifts between them."""
    return bool(
        z.climate and z.emitter == "fancoil" and z.fancoil_units and not z.follows
    )


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
        g = max(0.0, cooling_load(z.temp, state.outdoor_temp, state.solar, a=a, b=b, c=c))
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
