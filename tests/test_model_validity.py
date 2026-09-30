"""v0.73.0 model validity: plausibility + out-of-sample skill + actuator check.

The live rows below are the values read from sensor.*_model on 2026-09-30:
every one had confidence >= 0.95 and planner_eligible=true.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac.const import (
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    MODEL_MAX_A,
    MODEL_MAX_B,
    MODEL_MAX_C,
    MODEL_MAX_K,
    MODEL_MIN_K,
)
from custom_components.villa_hvac.policies import ThermalEstimator
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ParamBounds,
    ZoneSnapshot,
    rls_passive_update,
    seed_params,
)
from custom_components.villa_hvac.supervisor.thermal import (
    Plausibility,
    model_skill,
    model_validity,
)

BOUNDS = ParamBounds(MODEL_MAX_A, MODEL_MAX_B, MODEL_MAX_C, MODEL_MIN_K, MODEL_MAX_K)
LIMITS = Plausibility(
    max_a=0.08, max_b=0.002, max_c=0.3, max_k=1.5, min_skill=0.1, min_val=30
)
T0 = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = {"a": 0.006, "b": 0.0002, "c": 0.007, "k": 0.95, "p": [0.0] * 9,
            "p_k": 0.0, "n": 6000, "n_k": 120, "s_hi": 980.0,
            "err_ewma": 0.03, "ref_ewma": 0.1, "n_val": 500}
    base.update(kw)
    return base


def _validity(p, has_actuator=True):
    return model_validity(
        p, bounds=BOUNDS, limits=LIMITS, abc_conf_min=40, k_conf_min=20,
        has_actuator=has_actuator,
    )


# --- pure validity -------------------------------------------------------------

def test_a_healthy_fit_is_valid():
    p = replace(seed_params(0.006, 0.0002, 0.007, 0.95, p0_passive=(0.5, 1e-5, 4.0),
                            p0_k=4.0), n=6000, n_k=120, err_ewma=0.03,
                ref_ewma=0.1, n_val=500)
    v = _validity(p)
    assert v.abc_valid and v.k_valid and v.reasons == ()
    assert abs(v.skill - 0.7) < 1e-9


def test_saturated_and_implausible_fit_is_invalid():
    # the live salotto fit re-expressed inside the new box: pinned on a and c
    p = replace(seed_params(0.0999, 0.0019, 0.495, 1.2, p0_passive=(0.5, 1e-5, 4.0),
                            p0_k=4.0), n=6000, n_k=100, err_ewma=0.03,
                ref_ewma=0.1, n_val=500)
    v = _validity(p)
    assert not v.abc_valid and not v.k_valid
    assert {"saturated", "implausible_a", "implausible_c"} <= set(v.reasons)


def test_confident_but_no_skill_is_invalid():
    p = replace(seed_params(0.006, 0.0002, 0.007, 0.95, p0_passive=(0.5, 1e-5, 4.0),
                            p0_k=4.0), n=6000, n_k=120, err_ewma=0.1,
                ref_ewma=0.1, n_val=500)
    v = _validity(p)
    assert not v.abc_valid and "low_skill" in v.reasons
    assert model_skill(p) == 0.0


def test_no_working_actuator_invalidates_k_only():
    p = replace(seed_params(0.0007, 0.00009, 0.009, 0.45, p0_passive=(0.5, 1e-5, 4.0),
                            p0_k=4.0), n=6000, n_k=55, err_ewma=0.03,
                ref_ewma=0.1, n_val=500)
    v = _validity(p, has_actuator=False)
    assert v.abc_valid and not v.k_valid and "no_actuator" in v.reasons


def test_implausible_k_invalidates_k():
    p = replace(seed_params(0.006, 0.00002, 0.018, 1.79, p0_passive=(0.5, 1e-5, 4.0),
                            p0_k=4.0), n=6000, n_k=136, err_ewma=0.03,
                ref_ewma=0.1, n_val=500)
    v = _validity(p)
    assert v.abc_valid and not v.k_valid and "implausible_k" in v.reasons


def test_skill_tracks_the_a_priori_innovation():
    p = seed_params(0.03, 0.0008, 0.0, 1.2, p0_passive=(0.5, 1e-5, 4.0), p0_k=4.0)
    p2 = rls_passive_update(p, dt_dt=0.5, t_out=30.0, temp=24.0, solar=0.0,
                            forgetting=0.995, bounds=BOUNDS)
    # a-priori prediction 0.03*6 = 0.18 -> |err| = 0.32 ; |dT/dt| = 0.5
    assert p2.n_val == 1
    assert abs(p2.err_ewma - 0.32) < 1e-9 and abs(p2.ref_ewma - 0.5) < 1e-9


# --- estimator: live rows ----------------------------------------------------

def test_live_salotto_row_is_reseeded_on_load():
    est = ThermalEstimator()
    est.load({"living_room": _row(a=0.49716, b=0.009944, c=2.9819, k=1.2087,
                                  n=6579, n_k=116)})
    p = est.params["living_room"]
    assert (p.a, p.c, p.n, p.n_k) == (COOL_GAIN_OUTDOOR, COOL_GAIN_BASE, 0, 0)
    assert est.planner_eligible("living_room") is False


def test_live_sala_giochi_k_is_dropped_dead_fan():
    est = ThermalEstimator()
    est.load({"sala_giochi": _row(a=0.00072, b=0.000093, c=0.0092, k=0.453, n_k=55)})
    assert est.params["sala_giochi"].n_k == 0          # re-seeded on load
    v = est.validity("sala_giochi")
    assert not v.k_valid and "no_actuator" in v.reasons
    assert est.model_for("sala_giochi").k == COOL_CAPACITY


def test_live_gabriele_k_kept_but_not_trusted():
    est = ThermalEstimator()
    est.load({"gabriroom": _row(a=0.00617, b=0.000018, c=0.0179, k=1.7927, n_k=136)})
    assert est.params["gabriroom"].k == 1.7927          # inside the clamp: kept
    assert est.model_for("gabriroom").k == COOL_CAPACITY  # but control uses the prior
    assert est.planner_eligible("gabriroom") is False


def test_live_padronale_row_stays_valid():
    est = ThermalEstimator()
    est.load({"main_bedroom": _row(a=0.00576, b=0.000232, c=0.0066, k=0.9544,
                                   n_k=119)})
    v = est.validity("main_bedroom")
    assert v.abc_valid and v.k_valid
    assert est.planner_eligible("main_bedroom") is True


# --- estimator: what it learns from ------------------------------------------

def _leader(**kw):
    base = dict(
        zone_id="office", name="Office", climate="climate.office", emitter="fancoil",
        fancoil_units=(("fan.fancoil_studio_pianerottolo_p1", "switch.m"),),
        s_eff=0.0, temp=24.0, demand=False,
    )
    base.update(kw)
    return ZoneSnapshot(**base)


def _state(z, now, consenso="off"):
    return HouseState(now=now, zones={z.zone_id: z}, outdoor_temp=20.0, solar=0.0,
                      consenso_freddo=consenso, blocco="off")


def test_paused_room_windows_are_not_learned():
    est = ThermalEstimator()
    for m in range(0, 50, 1):
        z = _leader(temp=24.0 - 0.02 * m, paused=True)
        est.observe(_state(z, T0 + timedelta(minutes=m)))
    assert est.params["office"].n == 0
    for m in range(50, 100, 1):
        z = _leader(temp=24.0 - 0.005 * m)
        est.observe(_state(z, T0 + timedelta(minutes=m)))
    assert est.params["office"].n >= 1


def test_capacity_learned_from_auto_windows_with_known_fan():
    """k from a valve-OPEN window at a known, steady AUTO fan % (manuale OFF)."""
    est = ThermalEstimator()
    est.load({"office": _row(a=0.01, b=0.00001, c=0.09, k=0.34, n_k=10)})
    before = est.params["office"].n_k
    for m in range(0, 50):
        z = _leader(temp=26.0 - 0.01 * m, demand=True, fan_pct=100, manuale_on=False)
        est.observe(_state(z, T0 + timedelta(minutes=m), consenso="on"))
    assert est.params["office"].n_k > before
