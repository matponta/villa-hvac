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
    # D1: track the max window-mean solar over passive windows (b excitation). A
    # room whose passive windows were all sunless nights keeps s_hi ~ 0 -> b is
    # never identified -> not planner-eligible (though the count-based confidence,
    # used by the live blend, still rises — this gate is planner-only).
    s_hi = max(params.s_hi, solar) if solar >= 0 else params.s_hi
    return replace(params, a=a, b=b, c=c, p=new_p, n=params.n + 1, s_hi=s_hi)



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
