"""Tests for the pure Supervisor write-arbiter core (Phase A).

Covers the manual-override re-assert state machine (the #1 robustness risk:
distinguishing a dropped KNX telegram from a real hand change) plus the
priority merge and the tolerance compare.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from custom_components.villa_hvac.supervisor import (
    BLOCCO_BLOCK,
    BLOCCO_RELEASE,
    DEFAULT_OVERRIDE_BACKOFF,
    CoverInfo,
    DutyState,
    HouseState,
    LeverState,
    ParamBounds,
    RunPlan,
    ZoneSnapshot,
    abc_confidence,
    band_step,
    blend_params,
    build_plan,
    capacity_fan,
    cooling_load,
    cover_lever,
    duty_decision,
    estimate_rate,
    fan_level,
    k_confidence,
    merge_desired,
    plan_run,
    reconcile,
    rls_capacity_update,
    rls_passive_update,
    seed_params,
    temperature_lever,
    values_match,
)

T0 = datetime(2026, 6, 27, 12, 0, 0)


def _step(desired, current, state, now=T0, **kw):
    return reconcile(desired, current, state, now, **kw)


# --- values_match ------------------------------------------------------------

def test_values_match_numeric_within_and_outside_tolerance():
    assert values_match(24.2, 24.0, 0.3) is True
    assert values_match(24.0, 24.4, 0.3) is False
    assert values_match("24.2", 24.0, 0.3) is True  # string-numeric read


def test_values_match_strings_and_none():
    assert values_match("comfort", "comfort", 0.3) is True
    assert values_match("comfort", "economy", 0.3) is False
    assert values_match(None, None, 0.3) is True
    assert values_match(None, "comfort", 0.3) is False


# --- reconcile: basic transitions -------------------------------------------

def test_satisfied_no_write_when_already_at_desired():
    r = _step("economy", "economy", LeverState())
    assert r.write is None
    assert r.note == "satisfied"
    assert r.state.written == "economy"
    assert r.state.attempts == 0


def test_satisfied_within_setpoint_tolerance():
    r = _step(24.0, 24.2, LeverState(), tolerance=0.3)
    assert r.write is None
    assert r.note == "satisfied"


def test_first_write_when_diverged():
    r = _step("economy", "comfort", LeverState())
    assert r.write == "economy"
    assert r.note == "write"
    assert r.state.written == "economy"
    assert r.state.attempts == 1


def test_transient_read_is_ignored():
    for bad in ("unavailable", "unknown", None, ""):
        r = _step("economy", bad, LeverState(written="economy", attempts=1))
        assert r.write is None
        assert r.note == "transient"
        # state preserved — we neither write nor judge
        assert r.state == LeverState(written="economy", attempts=1)


def test_desired_none_releases_and_clears_tracking():
    r = _step(None, "comfort", LeverState(written="economy", attempts=2))
    assert r.write is None
    assert r.note == "released"
    assert r.state == LeverState()


# --- reconcile: the dropped-telegram vs manual-change discipline -------------

def test_dropped_telegram_converges_without_declaring_manual():
    """Write, then the read stays stale for a couple cycles (lost telegrams),
    then converges. Must re-assert and NEVER trip the override."""
    state = LeverState()
    # cycle 1: first write
    r = _step("economy", "comfort", state)
    assert (r.write, r.note, r.state.attempts) == ("economy", "write", 1)
    # cycle 2: still reads old (dropped) -> re-assert
    r = _step("economy", "comfort", r.state)
    assert (r.write, r.note, r.state.attempts) == ("economy", "reassert", 2)
    # cycle 3: still old -> re-assert again (attempts now == max)
    r = _step("economy", "comfort", r.state)
    assert (r.write, r.note, r.state.attempts) == ("economy", "reassert", 3)
    # cycle 4: telegram finally lands -> satisfied, override never tripped
    r = _step("economy", "economy", r.state)
    assert r.write is None
    assert r.note == "satisfied"
    assert r.state.override_until is None
    assert r.state.attempts == 0


def test_persistent_divergence_concedes_to_manual_after_reasserts():
    """A hand change that sticks: read stays at a foreign value through all
    re-asserts -> concede (override) and then hold off."""
    state = LeverState()
    r = _step("economy", "comfort", state)  # write
    r = _step("economy", "comfort", r.state)  # reassert (2)
    r = _step("economy", "comfort", r.state)  # reassert (3)
    r = _step("economy", "comfort", r.state)  # exhausted -> override
    assert r.write is None
    assert r.note == "override"
    assert r.state.override_until == T0 + DEFAULT_OVERRIDE_BACKOFF


def test_manual_hold_does_not_write_during_backoff():
    held = LeverState(override_until=T0 + timedelta(hours=1))
    r = _step("economy", "comfort", held, now=T0)
    assert r.write is None
    assert r.note == "manual-hold"
    assert r.state == held  # untouched


def test_backoff_expiry_resumes_control():
    expired = LeverState(override_until=T0 - timedelta(seconds=1))
    r = _step("economy", "comfort", expired, now=T0)
    # fresh reconcile after expiry -> writes desired again
    assert r.write == "economy"
    assert r.note == "write"
    assert r.state.override_until is None
    assert r.state.attempts == 1


def test_desired_change_midflight_rewrites_immediately():
    # we were asserting economy; now the policy wants comfort
    state = LeverState(written="economy", attempts=2)
    r = _step("comfort", "economy", state)
    assert r.write == "comfort"
    assert r.note == "write"
    assert r.state.written == "comfort"
    assert r.state.attempts == 1


def test_satisfied_then_diverge_reasserts_not_first_write():
    # reach satisfied, then a drop -> should re-assert path, not reset
    r = _step("economy", "economy", LeverState())  # satisfied, written=economy
    assert r.note == "satisfied"
    r2 = _step("economy", "comfort", r.state)  # diverged after being satisfied
    # written already == desired -> treated as re-assert (attempts 0 -> 1)
    assert r2.write == "economy"
    assert r2.note == "reassert"
    assert r2.state.attempts == 1


# --- merge_desired -----------------------------------------------------------

def test_merge_highest_priority_wins_per_lever():
    high = {"zoneA.preset": "building_protection"}  # e.g. window pause
    low = {"zoneA.preset": "comfort", "zoneB.preset": "economy"}  # house mode
    merged = merge_desired([high, low])
    assert merged == {
        "zoneA.preset": "building_protection",
        "zoneB.preset": "economy",
    }


def test_merge_explicit_release_none_wins_over_lower():
    high = {"blocco": None}  # explicit release by a higher policy
    low = {"blocco": "on"}
    assert merge_desired([high, low]) == {"blocco": None}


def test_merge_empty_outputs():
    assert merge_desired([]) == {}
    assert merge_desired([{}, {}]) == {}


# --- duty_decision (#9 central duty-cycle) -----------------------------------

MAX = timedelta(hours=2)
COOL = timedelta(minutes=30)


def test_duty_within_stint_allows():
    st, blocco = duty_decision(True, False, T0, DutyState(stint_start=T0), MAX, COOL)
    assert blocco == BLOCCO_RELEASE
    assert st.stint_start == T0 and st.cooloff_until is None


def test_duty_starts_stint_when_cooling_begins():
    st, blocco = duty_decision(True, False, T0, DutyState(), MAX, COOL)
    assert blocco == BLOCCO_RELEASE and st.stint_start == T0


def test_duty_stint_exceeded_starts_cooloff():
    st, blocco = duty_decision(
        True, False, T0, DutyState(stint_start=T0 - MAX), MAX, COOL
    )
    assert blocco == BLOCCO_BLOCK
    assert st.cooloff_until == T0 + COOL and st.stint_start is None


def test_duty_cooloff_holds_then_releases():
    cs = DutyState(cooloff_until=T0 + COOL)
    st, blocco = duty_decision(True, False, T0, cs, MAX, COOL)
    assert blocco == BLOCCO_BLOCK and st == cs  # still cooling off
    st2, blocco2 = duty_decision(True, False, T0 + COOL, cs, MAX, COOL)
    assert blocco2 == BLOCCO_RELEASE and st2 == DutyState()  # elapsed -> release


def test_duty_comfort_breach_aborts_cooloff():
    cs = DutyState(cooloff_until=T0 + COOL)
    st, blocco = duty_decision(True, True, T0, cs, MAX, COOL)
    assert blocco == BLOCCO_RELEASE and st == DutyState()


def test_duty_comfort_breach_prevents_cooloff_past_max():
    st, blocco = duty_decision(
        True, True, T0, DutyState(stint_start=T0 - MAX), MAX, COOL
    )
    assert blocco == BLOCCO_RELEASE and st == DutyState()  # comfort wins


def test_duty_not_cooling_releases_and_resets():
    st, blocco = duty_decision(False, False, T0, DutyState(stint_start=T0), MAX, COOL)
    assert blocco == BLOCCO_RELEASE and st == DutyState()


def test_duty_at_peak_never_blocks():
    # duty-adaptive: at peak, don't coalesce even past max stint.
    st, blocco = duty_decision(
        True, False, T0, DutyState(stint_start=T0 - MAX), MAX, COOL, at_peak=True
    )
    assert blocco == BLOCCO_RELEASE and st == DutyState()


def test_duty_precool_never_blocks():
    # forecast feed-forward: a peak is imminent -> bank coolth, don't rest.
    st, blocco = duty_decision(
        True, False, T0, DutyState(stint_start=T0 - MAX), MAX, COOL, precool=True
    )
    assert blocco == BLOCCO_RELEASE and st == DutyState()


# --- plan_run (#9 forecast planner, 12 h lookahead + margin gate) ------------

LOOK = timedelta(hours=12)


def _plan(fc, current, **kw):
    kw = {"peak_threshold": 30.0, "lookahead": LOOK, "margin": 3.0, **kw}
    return plan_run(fc, T0, current, **kw)


def test_plan_run_precool_when_hot_peak_and_currently_cool():
    fc = [(T0 + timedelta(hours=6), 34.0), (T0 + timedelta(hours=2), 28.0)]
    plan = _plan(fc, 25.0)  # 34 - 25 = 9 >= margin -> bank coolth now
    assert plan.precool is True and plan.forecast_peak == 34.0
    assert plan.peak_eta == timedelta(hours=6)


def test_plan_run_no_precool_when_already_near_peak():
    plan = _plan([(T0 + timedelta(hours=1), 34.0)], 32.0)  # 34-32=2 < margin
    assert plan.precool is False  # taper: peak-skip takes over near the peak


def test_plan_run_no_precool_without_a_hot_peak():
    plan = _plan([(T0 + timedelta(hours=2), 27.0)], 22.0)  # peak below threshold
    assert plan.precool is False and plan.forecast_peak == 27.0


def test_plan_run_respects_lookahead_window():
    plan = _plan([(T0 + timedelta(hours=15), 35.0)], 22.0)  # beyond 12 h
    assert plan.precool is False and plan.forecast_peak is None


def test_plan_run_no_precool_without_current_outdoor():
    plan = _plan([(T0 + timedelta(hours=3), 34.0)], None)
    assert plan.precool is False and plan.forecast_peak == 34.0


def test_plan_run_empty_forecast():
    assert _plan([], 25.0) == RunPlan()


# --- #3 v2 comfort-band control + capacity-matched fan -----------------------

# band B=1.5 (half=0.75), slam A=0.75, center (target) = 24.
BB = dict(band=1.5, slam=0.75)


def test_band_run_when_above_band():
    # 25.0 ≥ 24 + 0.75 -> RUN, setpoint slammed to center - A.
    phase, sp = band_step("released", eligible=True, temp=25.0, center=24.0, **BB)
    assert phase == "run" and sp == 23.25


def test_band_rest_when_below_band():
    phase, sp = band_step("released", eligible=True, temp=23.0, center=24.0, **BB)
    assert phase == "rest" and sp == 24.75


def test_band_holds_run_within_band():
    # in RUN, still inside the band (24.2) -> keep running (wide hysteresis).
    phase, sp = band_step("run", eligible=True, temp=24.2, center=24.0, **BB)
    assert phase == "run" and sp == 23.25


def test_band_run_flips_to_rest_only_at_lower_edge():
    # in RUN until temp drops to center - B/2 = 23.25.
    assert band_step("run", eligible=True, temp=23.3, center=24.0, **BB)[0] == "run"
    assert band_step("run", eligible=True, temp=23.2, center=24.0, **BB)[0] == "rest"


def test_band_released_when_ineligible():
    phase, sp = band_step("run", eligible=False, temp=25.0, center=24.0, **BB)
    assert phase == "released" and sp is None


# --- cooling_load + capacity_fan + fan_level ---------------------------------

CM = dict(a=0.03, b=0.0008, c=0.0)


def test_cooling_load_sums_terms():
    # 0.03*(32-24) + 0.0008*900 = 0.24 + 0.72 = 0.96
    assert round(cooling_load(24.0, 32.0, 900.0, **CM), 3) == 0.96


def test_cooling_load_drops_missing_terms():
    assert cooling_load(None, None, None, **CM) == 0.0


def test_fan_level_quantizes_and_floors():
    assert fan_level(0.64, 0) == 60          # 64% -> nearest 10
    assert fan_level(0.02, 10) == 10         # below floor -> fan_min
    assert fan_level(2.0, 0) == 100          # clamp to 100


def test_capacity_fan_matches_load():
    # (0.96 + 0.3)/1.2 = 1.05 -> clamps to 100 (peak: needs full fan)
    assert capacity_fan(0.96, pulldown=0.3, capacity=1.2, fan_min_pct=0) == 100
    # mild: (0.3 + 0.3)/1.2 = 0.5 -> 50%
    assert capacity_fan(0.3, pulldown=0.3, capacity=1.2, fan_min_pct=0) == 50


def test_capacity_fan_zero_capacity_is_full():
    assert capacity_fan(0.5, pulldown=0.3, capacity=0.0, fan_min_pct=0) == 100


# --- build_plan (#11 plan view) ----------------------------------------------


def _zone(zone_id, climate, **kw):
    base = dict(name=zone_id, emitter="fancoil", temp=25.0, enabled=True, paused=False)
    base.update(kw)
    return ZoneSnapshot(zone_id=zone_id, climate=climate, **base)


def _house(zones=(), **kw):
    opts = dict(
        season="summer",
        house_mode="Casa",
        house_setpoint=24.0,
        mode_offset=0.0,
        precool_offset=1.5,
        duty_enabled=True,
        duty_max_stint=timedelta(hours=2),
        duty_peak_outdoor=30.0,
        outdoor_temp=26.0,
        consenso_freddo="off",
    )
    opts.update(kw)
    return HouseState(now=T0, zones={z.zone_id: z for z in zones}, **opts)


def test_build_plan_summary_idle_when_nothing_active():
    plan = build_plan(_house(), RunPlan(), {}, DutyState(), [], LOOK)
    assert plan.summary == "idle"
    assert plan.cooling is False and plan.precool is False


def test_build_plan_summary_cooling():
    plan = build_plan(
        _house(consenso_freddo="on"), RunPlan(), {}, DutyState(), [], LOOK
    )
    assert plan.summary == "cooling" and plan.cooling is True


def test_build_plan_summary_precool_and_setpoint():
    rp = RunPlan(precool=True, forecast_peak=34.0, peak_eta=timedelta(hours=6))
    plan = build_plan(_house(), rp, {}, DutyState(), [], LOOK)
    assert plan.summary == "pre_cool"
    assert plan.effective_setpoint == 24.0
    assert plan.precool_setpoint == 22.5  # 24 - 1.5
    assert plan.forecast_peak == 34.0 and plan.peak_eta == timedelta(hours=6)


def test_build_plan_summary_peak_run_when_hot_outside():
    plan = build_plan(_house(outdoor_temp=33.0), RunPlan(), {}, DutyState(), [], LOOK)
    assert plan.at_peak is True and plan.summary == "peak_run"


def test_build_plan_summary_duty_rest_in_cooloff():
    duty = DutyState(cooloff_until=T0 + timedelta(minutes=20))
    plan = build_plan(
        _house(consenso_freddo="on"), RunPlan(), {}, duty, [], LOOK
    )
    assert plan.in_cooloff is True and plan.summary == "duty_rest"
    assert plan.cooloff_until == T0 + timedelta(minutes=20)
    assert plan.blocco_desired == BLOCCO_BLOCK


def test_build_plan_summary_free_cool_wins():
    plan = build_plan(
        _house(free_cool_enabled=True, free_cool_threshold=22.0, outdoor_temp=20.0),
        RunPlan(), {}, DutyState(), [], LOOK,
    )
    assert plan.free_cool is True and plan.summary == "free_cool"


def test_build_plan_zone_targets_from_desired():
    z = _zone("living_room", "climate.salotto")
    desired = {temperature_lever("climate.salotto"): 23.0}
    plan = build_plan(_house([z]), RunPlan(), desired, DutyState(), [], LOOK)
    (zp,) = plan.zones
    assert zp.zone_id == "living_room" and zp.target == 23.0 and zp.temp == 25.0


def test_build_plan_covers_closing_from_desired():
    cover = CoverInfo(entity_id="cover.salotto", orientation="west", zone="lr")
    desired = {cover_lever("cover.salotto"): "closed"}
    plan = build_plan(
        _house(covers=(cover,)), RunPlan(), desired, DutyState(), [], LOOK
    )
    assert plan.covers_closing == ("cover.salotto",)


def test_build_plan_windows_the_forecast_curve():
    fc = [
        (T0 + timedelta(hours=2), 30.0),
        (T0 + timedelta(hours=15), 35.0),  # beyond the 12 h lookahead
        (T0 - timedelta(hours=1), 24.0),   # in the past
    ]
    plan = build_plan(_house(), RunPlan(), {}, DutyState(), fc, LOOK)
    assert plan.forecast == ((T0 + timedelta(hours=2), 30.0),)


def test_build_plan_stint_elapsed_tracks_duty():
    duty = DutyState(stint_start=T0 - timedelta(minutes=45))
    plan = build_plan(_house(consenso_freddo="on"), RunPlan(), {}, duty, [], LOOK)
    assert plan.stint_elapsed == timedelta(minutes=45)
    assert plan.stint_cap == timedelta(hours=2)


def test_build_plan_winter_reflects_heating_call():
    plan = build_plan(
        _house(season="winter", consenso_caldo="on", consenso_freddo="off"),
        RunPlan(), {}, DutyState(), [], LOOK,
    )
    assert plan.summary == "heating"


# --- F2: online thermal-model estimator (pure RLS) ---------------------------

_BOUNDS = ParamBounds(max_a=0.5, max_b=0.01, max_c=3.0, min_k=0.1, max_k=5.0)


def _seed():
    return seed_params(0.0, 0.0, 0.0, 1.2, p0_passive=(1.0, 1e-4, 4.0), p0_k=4.0)


def test_rls_passive_converges_to_truth():
    truth_a, truth_b, truth_c = 0.04, 0.0012, 0.2
    p = _seed()
    deltas = [2.0, 5.0, 8.0, 3.0, 6.0, 1.0, 7.0, 4.0]
    solars = [100.0, 400.0, 700.0, 200.0, 900.0, 0.0, 600.0, 300.0]
    for i in range(300):
        d = deltas[i % len(deltas)]
        s = solars[(i * 3) % len(solars)]
        temp, t_out = 24.0, 24.0 + d
        y = truth_a * d + truth_b * s + truth_c  # noise-free dT/dt
        p = rls_passive_update(
            p, dt_dt=y, t_out=t_out, temp=temp, solar=s,
            forgetting=0.995, bounds=_BOUNDS,
        )
    assert abs(p.a - truth_a) < 0.01
    assert abs(p.b - truth_b) < 5e-4
    assert abs(p.c - truth_c) < 0.1
    assert p.n == 300 and p.n_k == 0  # passive only; k untouched


def test_rls_capacity_converges_holding_abc():
    p = _seed()  # a=b=c=0 -> G=0 -> dT/dt = -k*u
    truth_k = 1.0
    us = [0.3, 0.5, 1.0, 0.7, 0.4]
    for i in range(200):
        u = us[i % len(us)]
        p = rls_capacity_update(
            p, dt_dt=-truth_k * u, t_out=30.0, temp=24.0, solar=0.0, u=u,
            forgetting=0.995, bounds=_BOUNDS,
        )
    assert abs(p.k - truth_k) < 0.05
    assert p.a == 0.0 and p.b == 0.0 and p.c == 0.0  # passive untouched


def test_rls_passive_clamps_to_physical_bounds():
    p = seed_params(0.04, 0.001, 0.2, 1.2, p0_passive=(1.0, 1e-4, 4.0), p0_k=4.0)
    # a wildly negative rate with positive (T_out-T) would push a < 0 -> clamp.
    p2 = rls_passive_update(
        p, dt_dt=-50.0, t_out=34.0, temp=24.0, solar=0.0,
        forgetting=0.995, bounds=_BOUNDS,
    )
    assert p2.a >= 0.0 and p2.b >= 0.0 and p2.c >= 0.0


def test_rls_capacity_clamps_k_positive():
    p = _seed()
    # huge positive dT/dt while cooling would push k negative -> clamp to min_k.
    p2 = rls_capacity_update(
        p, dt_dt=10.0, t_out=30.0, temp=24.0, solar=0.0, u=1.0,
        forgetting=0.995, bounds=_BOUNDS,
    )
    assert p2.k >= _BOUNDS.min_k


def test_rls_rejects_non_finite():
    p = _seed()
    assert rls_passive_update(
        p, dt_dt=float("nan"), t_out=30.0, temp=24.0, solar=0.0,
        forgetting=0.995, bounds=_BOUNDS,
    ) is p
    assert rls_capacity_update(
        p, dt_dt=-0.5, t_out=30.0, temp=24.0, solar=0.0, u=0.0,  # u<=0 -> no info
        forgetting=0.995, bounds=_BOUNDS,
    ) is p


def test_estimate_rate_slope_and_min_span():
    base = datetime(2026, 6, 30, 12, 0, 0)
    samples = [
        (base + timedelta(minutes=m), 24.0 + 1.2 * (m / 60.0)) for m in range(0, 21, 2)
    ]
    assert abs(estimate_rate(samples, min_span_h=0.25) - 1.2) < 0.01
    # a 4-minute span (< 15 min) is rejected — never trust a quantization-noisy diff.
    short = [(base + timedelta(minutes=m), 24.0) for m in (0, 2, 4)]
    assert estimate_rate(short, min_span_h=0.25) is None
    # fewer than 3 points -> None.
    assert estimate_rate([(base, 24.0)], min_span_h=0.25) is None


def test_blend_uses_prior_below_confidence_learned_above():
    prior = seed_params(0.03, 0.0008, 0.0, 1.2, p0_passive=(1.0, 1e-4, 4.0), p0_k=4.0)
    fresh = replace(prior, a=0.1, k=0.5, n=0, n_k=0)
    below = blend_params(fresh, prior, abc_conf_min=40, k_conf_min=20)
    assert abs(below.a - prior.a) < 1e-9 and abs(below.k - prior.k) < 1e-9
    converged = replace(prior, a=0.1, k=0.5, n=400, n_k=200)
    above = blend_params(converged, prior, abc_conf_min=40, k_conf_min=20)
    assert above.a > 0.08 and above.k < 0.7  # mostly the learned values


def test_confidence_monotone():
    p = replace(_seed(), n=40, n_k=20)
    assert abs(abc_confidence(p, conf_min=40) - 0.5) < 1e-9
    assert abs(k_confidence(p, conf_min=20) - 0.5) < 1e-9


def test_capacity_fan_hysteresis_holds_level():
    # raw 45% near last level 50, within step/2+hyst (10) -> hold 50 (no hunt).
    assert capacity_fan(0.45, pulldown=0.0, capacity=1.0, fan_min_pct=0,
                        last_level=50, hysteresis=5) == 50
    # raw 35% is beyond the boundary -> step down to 40.
    assert capacity_fan(0.35, pulldown=0.0, capacity=1.0, fan_min_pct=0,
                        last_level=50, hysteresis=5) == 40


# --- F3a: house load index + regime selector ---------------------------------

from custom_components.villa_hvac.supervisor import (  # noqa: E402
    REGIME_LOW, REGIME_MEDIUM, REGIME_PEAK, HouseLoad,
    house_load_index, select_regime,
)

_DEF = dict(default_a=0.03, default_b=0.0008, default_c=0.0, default_capacity=1.2,
            k_conf_min=0.5)


def _hz(zid, *, temp, model_k=None, kconf=None, enabled=True, paused=False,
        bedroom=False):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter="fancoil",
        fancoil_units=((f"fan.{zid}", f"switch.{zid}_man"),),
        temp=temp, enabled=enabled, paused=paused, bedroom=bedroom,
        model_k=model_k, model_k_confidence=kconf,
    )


def _hstate(zones, *, outdoor=33.0, solar=600.0, night=False):
    return HouseState(now=T0, zones={z.zone_id: z for z in zones},
                      outdoor_temp=outdoor, solar=solar, night_active=night)


def test_house_load_index_counts_only_converged_k():
    # one converged zone, one still on priors (kconf None) -> only the first
    # contributes to the ratio; both appear in per_zone.
    zones = [
        _hz("a", temp=26.0, model_k=0.8, kconf=0.9),  # converged
        _hz("b", temp=26.0),                           # prior k, not counted
    ]
    load = house_load_index(_hstate(zones), **_DEF)
    assert load.n_eligible == 1
    assert "a" in load.per_zone and "b" in load.per_zone
    assert load.k_house == 0.8  # only the converged zone's k


def test_house_load_index_excludes_disabled_paused_night_bedroom():
    zones = [
        _hz("dis", temp=26.0, model_k=0.8, kconf=0.9, enabled=False),
        _hz("pause", temp=26.0, model_k=0.8, kconf=0.9, paused=True),
        _hz("bed", temp=26.0, model_k=0.8, kconf=0.9, bedroom=True),
    ]
    load = house_load_index(_hstate(zones, night=True), **_DEF)
    assert load.n_eligible == 0 and load.per_zone == {}


def test_select_regime_free_cool_and_peak_and_low_on_priors():
    # priors -> no converged k -> n_eligible 0 -> LOW unless at_peak.
    load = HouseLoad(g_house=0.0, k_house=0.0, load_ratio=0.0, n_eligible=0)
    assert select_regime(load, at_peak=False, free_cool=True,
                         peak_ratio=0.85, medium_ratio=0.1) == REGIME_LOW
    assert select_regime(load, at_peak=True, free_cool=False,
                         peak_ratio=0.85, medium_ratio=0.1) == REGIME_PEAK
    assert select_regime(load, at_peak=False, free_cool=False,
                         peak_ratio=0.85, medium_ratio=0.1) == REGIME_LOW


def test_select_regime_medium_and_peak_when_converged():
    medium = HouseLoad(g_house=0.5, k_house=1.6, load_ratio=0.31, n_eligible=2)
    assert select_regime(medium, at_peak=False, free_cool=False,
                         peak_ratio=0.85, medium_ratio=0.1) == REGIME_MEDIUM
    peak = HouseLoad(g_house=1.5, k_house=1.6, load_ratio=0.94, n_eligible=2)
    assert select_regime(peak, at_peak=False, free_cool=False,
                         peak_ratio=0.85, medium_ratio=0.1) == REGIME_PEAK


# --- F3b: 12h per-room forward simulation + precool --------------------------

import math  # noqa: E402

from custom_components.villa_hvac.supervisor import (  # noqa: E402
    RoomParams, build_room_plans, peak_window, schedule_precool, simulate_room,
)

_NOW = datetime(2026, 6, 30, 8, 0, 0)
_LOOK = timedelta(hours=12)
_RP = RoomParams(a=0.03, b=0.0008, c=0.0, k=1.2, pulldown=0.3, fan_min=0)


def _fc(peak=35.0, base=26.0):
    # hourly forecast rising to `peak` at +6h then falling.
    return [
        (_NOW + timedelta(hours=h), round(base + (peak - base) * (1 - abs(h - 6) / 6), 2))
        for h in range(13)
    ]


def test_peak_window_argmax_and_empty():
    pk = peak_window(_fc(35.0), _NOW, _LOOK)
    assert pk is not None and pk[1] == 35.0
    assert peak_window([], _NOW, _LOOK) is None


def test_simulate_room_step0_matches_band_step():
    tr = simulate_room(
        zone_id="a", params=_RP, t0=26.0, center=24.0, band=1.5, slam=0.75,
        forecast=_fc(), solar=None, now=_NOW, lookahead=_LOOK, dt_min=15,
    )
    ph, sp = band_step("run", eligible=True, temp=26.0, center=24.0, band=1.5, slam=0.75)
    assert tr.points[0].phase == ph and abs(tr.points[0].setpoint - sp) < 1e-6
    assert tr.points[0].minute == 0 and tr.points[-1].minute == 12 * 60


def test_simulate_room_water_gate_no_cooling():
    n = int(_LOOK.total_seconds() / 60 // 15) + 1
    tr = simulate_room(
        zone_id="a", params=_RP, t0=24.0, center=24.0, band=1.5, slam=0.75,
        forecast=_fc(), solar=[800.0] * n, now=_NOW, lookahead=_LOOK,
        water_available=[False] * n, dt_min=15,
    )
    assert tr.max_temp >= 24.0  # no chilled water + sun -> only warms


def test_simulate_room_large_k_stays_bounded():
    rp = RoomParams(a=0.1, b=0.0, c=0.0, k=4.0, pulldown=0.3, fan_min=0)
    tr = simulate_room(
        zone_id="a", params=rp, t0=28.0, center=24.0, band=1.5, slam=0.75,
        forecast=_fc(), solar=None, now=_NOW, lookahead=_LOOK, dt_min=15,
    )
    assert all(math.isfinite(p.temp) for p in tr.points)
    assert -10.0 < tr.points[-1].temp < 50.0  # sub-stepping prevents blow-up


def test_schedule_precool_no_breach_depth_zero():
    rp = RoomParams(a=0.02, b=0.0, c=0.0, k=2.0, pulldown=0.3, fan_min=0)
    tr = schedule_precool(
        zone_id="a", params=rp, t0=24.0, center=24.0, band=1.5, slam=0.75,
        forecast=_fc(30.0), solar=None, now=_NOW, lookahead=_LOOK,
        max_depth=3.0, dt_min=15,
    )
    assert tr.precool_depth == 0.0 and tr.peak_breach is False


def test_schedule_precool_gain_limited_flags_breach():
    # harsh params (huge gain, tiny capacity) -> even max precool can't hold.
    rp = RoomParams(a=0.5, b=0.005, c=0.5, k=0.3, pulldown=0.3, fan_min=0)
    n = int(_LOOK.total_seconds() / 60 // 15) + 1
    tr = schedule_precool(
        zone_id="a", params=rp, t0=27.0, center=24.0, band=1.5, slam=0.75,
        forecast=_fc(36.0), solar=[700.0] * n, now=_NOW, lookahead=_LOOK,
        max_depth=3.0, dt_min=15,
    )
    assert tr.peak_breach is True


def test_build_room_plans_one_per_leader_downsampled():
    z = ZoneSnapshot(
        zone_id="lr", name="lr", climate="climate.lr", emitter="fancoil",
        fancoil_units=(("fan.lr", "switch.lr_man"),), temp=26.0,
        model_a=0.03, model_b=0.0008, model_c=0.0, model_k=1.2,
    )
    st = HouseState(
        now=_NOW, zones={"lr": z}, house_setpoint=24.0, mode_offset=0.0,
        band_width=1.5, band_slam=0.75, outdoor_temp=30.0, solar=200.0,
    )
    trs = build_room_plans(
        st, {"lr": _RP}, _fc(), [200.0] * 60, _LOOK, dt_min=15, downsample_min=60,
    )
    assert len(trs) == 1 and trs[0].zone_id == "lr"
    assert len(trs[0].points) <= 14  # downsampled to ~hourly (not 49 macro steps)


# --- F4a: solar forecast -----------------------------------------------------

from custom_components.villa_hvac.supervisor import (  # noqa: E402
    clear_sky_solar, solar_forecast_curve,
)


def test_clear_sky_solar_zero_at_night_and_scales():
    assert clear_sky_solar(elevation_deg=-5.0, clear_sky_ghi=950.0, cloud_fraction=0.0) == 0.0
    noon = clear_sky_solar(elevation_deg=90.0, clear_sky_ghi=950.0, cloud_fraction=0.0)
    assert abs(noon - 950.0) < 1e-6  # sin(90)=1, no cloud
    half = clear_sky_solar(elevation_deg=30.0, clear_sky_ghi=950.0, cloud_fraction=0.0)
    assert abs(half - 475.0) < 1.0   # sin(30)=0.5


def test_clear_sky_solar_cloud_and_missing():
    clear = clear_sky_solar(elevation_deg=45.0, clear_sky_ghi=900.0, cloud_fraction=0.0)
    cloudy = clear_sky_solar(elevation_deg=45.0, clear_sky_ghi=900.0, cloud_fraction=0.6)
    assert cloudy < clear and abs(cloudy - clear * 0.4) < 1e-6
    # missing cloud -> assume clear (== no cloud)
    miss = clear_sky_solar(elevation_deg=45.0, clear_sky_ghi=900.0, cloud_fraction=None)
    assert abs(miss - clear) < 1e-6


def test_solar_forecast_curve_zeroes_night_and_guards_cloud():
    curve = solar_forecast_curve(
        elevations=[-10.0, 20.0, 60.0], clouds=[0.0, None, 0.5], clear_sky_ghi=950.0,
    )
    assert curve[0] == 0.0          # below horizon
    assert curve[1] > 0.0           # missing cloud -> clear
    assert curve[2] > 0.0 and curve[2] < 950.0


# --- F4b: comfort windows ----------------------------------------------------

from custom_components.villa_hvac.supervisor import in_window  # noqa: E402


def test_in_window_normal_and_outside():
    assert in_window(600, 480, 1380) is True     # 10:00 in 08:00-23:00
    assert in_window(1400, 480, 1380) is False    # 23:20 outside


def test_in_window_wraps_midnight():
    assert in_window(1380, 1320, 480) is True     # 23:00 in 22:00-08:00 (wrap)
    assert in_window(60, 1320, 480) is True        # 01:00 in the night window
    assert in_window(600, 1320, 480) is False      # 10:00 not in the night window


def test_in_window_equal_bounds_is_always():
    assert in_window(123, 600, 600) is True


# --- F3c: coalescing (pure) --------------------------------------------------

from custom_components.villa_hvac.supervisor import (  # noqa: E402
    RegimeState, coalesce_phase, run_rest_durations,
)

_MON = timedelta(minutes=10)
_CK = dict(center=24.0, band=1.5, min_on=_MON, min_off=_MON,
           enter_frac=0.5, exit_frac=0.5)


def test_coalesce_rest_holds_below_enter():
    rs = RegimeState(house_phase="rest", rest_started=T0 - timedelta(hours=1))
    _, ph = coalesce_phase(rs, room_temps={"a": 24.2}, now=T0, comfort_breach=False, **_CK)
    assert ph == "rest"  # 24.2 < enter (24.375)


def test_coalesce_rest_to_run_when_hot_and_min_off_elapsed():
    rs = RegimeState(house_phase="rest", rest_started=T0 - timedelta(hours=1))
    ns, ph = coalesce_phase(rs, room_temps={"a": 25.0}, now=T0, comfort_breach=False, **_CK)
    assert ph == "run" and ns.run_started == T0


def test_coalesce_min_off_blocks_run():
    rs = RegimeState(house_phase="rest", rest_started=T0 - timedelta(minutes=2))
    _, ph = coalesce_phase(rs, room_temps={"a": 25.0}, now=T0, comfort_breach=False, **_CK)
    assert ph == "rest"  # min_off not yet elapsed


def test_coalesce_comfort_breach_forces_run():
    rs = RegimeState(house_phase="rest", rest_started=T0 - timedelta(minutes=1))
    _, ph = coalesce_phase(rs, room_temps={"a": 24.0}, now=T0, comfort_breach=True, **_CK)
    assert ph == "run"  # breach overrides min_off


def test_coalesce_run_to_rest_only_when_all_cool():
    rs = RegimeState(house_phase="run", run_started=T0 - timedelta(hours=1))
    # a fast room is cool but a slow room is still warm -> stay RUN (never force-rest)
    _, ph = coalesce_phase(
        rs, room_temps={"a": 23.4, "b": 24.6}, now=T0, comfort_breach=False, **_CK
    )
    assert ph == "run"
    # ALL rooms cool -> REST
    _, ph2 = coalesce_phase(
        rs, room_temps={"a": 23.4, "b": 23.5}, now=T0, comfort_breach=False, **_CK
    )
    assert ph2 == "rest"


def test_run_rest_durations():
    run, rest = run_rest_durations(0.5, 1.2, 1.0, 1.5)
    assert run is not None and rest is not None
    assert run_rest_durations(1.5, 1.2, 1.0, 1.5)[0] is None   # net<=0 -> no run
    assert run_rest_durations(0.0, 1.2, 1.0, 1.5)[1] is None   # g=0 -> no rest
