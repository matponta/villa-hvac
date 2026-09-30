"""Pure thermal model (C2 split): the online RLS estimators (F2) + the
prior->learned confidence blend. Bounded + NaN-rejecting."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import math




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
    s_hi: float = 0.0                   # D1: max window-mean solar over passive
    #                                     windows (solar-excitation of b). abc is
    #                                     only planner-trustworthy once this crosses
    #                                     the excitation threshold.
    # v0.73.0 out-of-sample skill: EWMA of the A-PRIORI |prediction error| of
    # the passive model (computed BEFORE the update, so the model has not seen
    # the sample) vs the EWMA |dT/dt| of the trivial "no change" predictor.
    # skill = 1 - err/ref. Count-based confidence says "stopped moving"; skill
    # says "actually predicts" — the salotto fit had confidence 0.994 while
    # every coefficient sat pinned on its clamp.
    err_ewma: float = 0.0
    ref_ewma: float = 0.0
    n_val: int = 0



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
    # v0.73.0 skill bookkeeping on the a-priori innovation (honest: pre-update).
    n_val = params.n_val + 1
    alpha = max(SKILL_ALPHA, 1.0 / n_val)   # plain mean until 1/alpha samples
    err_ewma = params.err_ewma + alpha * (abs(err) - params.err_ewma)
    ref_ewma = params.ref_ewma + alpha * (abs(dt_dt) - params.ref_ewma)
    a = _clamp(theta[0] + gain[0] * err, 0.0, bounds.max_a)
    b = _clamp(theta[1] + gain[1] * err, 0.0, bounds.max_b)
    c = _clamp(theta[2] + gain[2] * err, 0.0, bounds.max_c)
    new_p = tuple(
        (p[3 * i + j] - gain[i] * px[j]) / forgetting
        for i in range(3) for j in range(3)
    )
    if not all(math.isfinite(v) for v in (a, b, c, *new_p)):
        return params
    # D1: track the max window-mean solar over passive windows (b excitation). A
    # room whose passive windows were all sunless nights keeps s_hi ~ 0 -> b is
    # never identified -> not planner-eligible (though the count-based confidence,
    # used by the live blend, still rises — this gate is planner-only).
    s_hi = max(params.s_hi, solar) if solar >= 0 else params.s_hi
    return replace(
        params, a=a, b=b, c=c, p=new_p, n=params.n + 1, s_hi=s_hi,
        err_ewma=err_ewma, ref_ewma=ref_ewma, n_val=n_val,
    )



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



# --- v0.73.0 model validity (system review 2026-09-30 §3) ---------------------
SKILL_ALPHA = 0.02            # EWMA weight of the skill trackers (~50 windows)
# Saturation: a coefficient within this fraction of its clamp is not "learned",
# it is the clamp — the estimator has diverged into the corner of the box.
SATURATION_FRACTION = 0.98


@dataclass(frozen=True)
class Plausibility:
    """Physical plausibility limits for a room's grey-box params (NOT the RLS
    clamps: a value inside the clamp box can still be physically absurd)."""

    max_a: float      # 1/h  — a=0.08 is a 12.5 h envelope time constant
    max_b: float      # °C/h per W/m²
    max_c: float      # °C/h — internal gain with no sun, no outdoor drive
    max_k: float      # °C/h at 100 % — measured best 0.85 (padronale)
    min_skill: float  # out-of-sample skill needed to trust {a,b,c}
    min_val: int      # a-priori innovations needed before skill is judged


@dataclass(frozen=True)
class ModelValidity:
    abc_valid: bool
    k_valid: bool
    skill: float | None
    reasons: tuple[str, ...]


def model_skill(params: ThermalParams) -> float | None:
    """1 - err/ref over the a-priori innovations; None until any were seen."""
    if params.n_val <= 0 or params.ref_ewma <= 0:
        return None
    return 1.0 - params.err_ewma / params.ref_ewma


def model_validity(
    params: ThermalParams, *, bounds: ParamBounds, limits: Plausibility,
    abc_conf_min: float, k_conf_min: float, has_actuator: bool,
) -> ModelValidity:
    """Is this room's learned model fit to FEED control/planning? Pure.

    {a,b,c}: enough data, not pinned on a clamp, physically plausible, and it
    beats the trivial "no change" predictor out of sample. k: the room has a
    working cooling actuator, {a,b,c} is valid (k_obs is derived from G), and k
    is plausible + has enough windows. Invalid parts fall back to the priors."""
    reasons: list[str] = []
    skill = model_skill(params)
    if params.n < abc_conf_min or params.n_val < limits.min_val:
        reasons.append("insufficient_data")
    if (
        params.a >= SATURATION_FRACTION * bounds.max_a
        or params.b >= SATURATION_FRACTION * bounds.max_b
        or params.c >= SATURATION_FRACTION * bounds.max_c
    ):
        reasons.append("saturated")
    if params.a > limits.max_a:
        reasons.append("implausible_a")
    if params.b > limits.max_b:
        reasons.append("implausible_b")
    if params.c > limits.max_c:
        reasons.append("implausible_c")
    if (
        params.n_val >= limits.min_val
        and (skill is None or skill < limits.min_skill)
    ):
        reasons.append("low_skill")
    abc_valid = not reasons
    k_reasons: list[str] = []
    if not has_actuator:
        k_reasons.append("no_actuator")
    if not abc_valid:
        k_reasons.append("abc_invalid")
    if params.k > limits.max_k or params.k >= SATURATION_FRACTION * bounds.max_k:
        k_reasons.append("implausible_k")
    if params.n_k < k_conf_min:
        k_reasons.append("k_insufficient_data")
    reasons.extend(r for r in k_reasons if r != "abc_invalid")
    return ModelValidity(
        abc_valid=abc_valid, k_valid=not k_reasons, skill=skill,
        reasons=tuple(reasons),
    )


def reseed_passive(params: ThermalParams, prior: ThermalParams) -> ThermalParams:
    """Drop a diverged {a,b,c} back to the prior (fresh covariance, counts and
    skill reset). k is reset too: its observations were residuals of the bad G."""
    return replace(
        params, a=prior.a, b=prior.b, c=prior.c, p=prior.p, n=0, s_hi=0.0,
        err_ewma=0.0, ref_ewma=0.0, n_val=0,
        k=prior.k, p_k=prior.p_k, n_k=0,
    )


def reseed_capacity(params: ThermalParams, prior: ThermalParams) -> ThermalParams:
    return replace(params, k=prior.k, p_k=prior.p_k, n_k=0)


def abc_confidence(params: ThermalParams, *, conf_min: float) -> float:
    """0→1 trust in the learned {a,b,c}, crossing 0.5 at conf_min updates."""
    total = params.n + conf_min
    return params.n / total if total > 0 else 0.0



def k_confidence(params: ThermalParams, *, conf_min: float) -> float:
    """0→1 trust in the learned k, crossing 0.5 at conf_min updates."""
    total = params.n_k + conf_min
    return params.n_k / total if total > 0 else 0.0


def abc_identified(
    params: ThermalParams, *, conf_min: float, solar_excitation_min: float
) -> bool:
    """D1: is {a,b,c} trustworthy for the PLANNER? True only when the count-based
    confidence has crossed 0.5 (n >= conf_min) AND the passive windows actually
    excited b (max window-mean solar >= solar_excitation_min). Guards against a `b`
    fit only on sunless nights (the gain-limited-room failure mode)."""
    return (
        abc_confidence(params, conf_min=conf_min) >= 0.5
        and params.s_hi >= solar_excitation_min
    )


def planner_eligible(
    params: ThermalParams, *,
    abc_conf_min: float, k_conf_min: float, solar_excitation_min: float,
    k_confidence_min: float = 0.5,
) -> bool:
    """D1: may the unified planner's reference drive THIS room's center? Only when
    {a,b,c} is identified (excited) AND k has converged (> 0 and confidence
    crossed). Hard gain-limited rooms rarely produce a held-fan k window, so their
    k stays a night-calibrated lower bound and this stays False -> their planner
    trajectories remain ADVISORY (comfort is always held by the reactive band)."""
    return (
        abc_identified(
            params, conf_min=abc_conf_min, solar_excitation_min=solar_excitation_min
        )
        and params.k > 0
        and k_confidence(params, conf_min=k_conf_min) >= k_confidence_min
    )



def rebase_solar_units(
    params: ThermalParams, *, prior_b: float, p0_b: float
) -> ThermalParams:
    """S_eff units rebase (STORY_SEFF §4.2): the solar regressor's input
    semantics changed (GHI ↔ a facade tag), so the learned b is meaningless in
    the new units. Wipe b to the PRIOR (never 0: blend_params applies the
    shared count weight to a, b, c jointly, so with high kept n a zeroed b
    would be trusted — reset-to-prior makes blended b == prior exactly while
    RLS re-identifies), reopen b's covariance row/col only (the textbook
    single-parameter reset: the gain routes prediction error almost entirely
    into b while a, c stay pinned), and zero s_hi (it measured excitation of
    the OLD regressor; keeping it would fake abc_identified/planner_eligible
    on a wiped b — this also auto-suspends k learning via the identified
    gate). a, c, k, n, n_k, p_k are KEPT: their regressors did not change."""
    p = params.p
    new_p = (
        p[0], 0.0, p[2],
        0.0, p0_b, 0.0,
        p[6], 0.0, p[8],
    )
    return replace(params, b=prior_b, p=new_p, s_hi=0.0)


def blend_params(
    learned: ThermalParams, prior: ThermalParams, *,
    abc_conf_min: float, k_conf_min: float,
    solar_excitation_min: float | None = None,
) -> ThermalParams:
    """Hand control from the prior to the learned model as confidence grows: each
    coefficient = prior·(1−w) + learned·w, with separate weights for {a,b,c} and
    k. Below confidence the prior dominates → control behaves exactly like F1
    until a room's model has actually converged."""
    wa = abc_confidence(learned, conf_min=abc_conf_min)
    wk = k_confidence(learned, conf_min=k_conf_min)
    # k is only meaningful while the passive gain model that produced its
    # residual is currently identified.  A units rebase or lost excitation
    # keeps the learned value stored, but control falls back to the safe prior.
    if solar_excitation_min is not None and not abc_identified(
        learned,
        conf_min=abc_conf_min,
        solar_excitation_min=solar_excitation_min,
    ):
        wk = 0.0
    return replace(
        learned,
        a=prior.a * (1 - wa) + learned.a * wa,
        b=prior.b * (1 - wa) + learned.b * wa,
        c=prior.c * (1 - wa) + learned.c * wa,
        k=prior.k * (1 - wk) + learned.k * wk,
    )
