"""Tests for the pure Supervisor write-arbiter core (Phase A).

Covers the manual-override re-assert state machine (the #1 robustness risk:
distinguishing a dropped KNX telegram from a real hand change) plus the
priority merge and the tolerance compare.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.villa_hvac.supervisor import (
    BLOCCO_BLOCK,
    BLOCCO_RELEASE,
    DEFAULT_OVERRIDE_BACKOFF,
    CoverInfo,
    DutyState,
    HouseState,
    LeverState,
    RunPlan,
    ZoneSnapshot,
    build_plan,
    cover_lever,
    duty_decision,
    merge_desired,
    pacing_decision,
    plan_run,
    reconcile,
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


# --- pacing_decision (#3 fan pacing) -----------------------------------------

PB = dict(approach_band=1.0, maintain_band=0.3, approach_pct=100, maintain_pct=33)


def test_pacing_pulls_down_when_far():
    assert pacing_decision("approach", 2.0, **PB) == ("approach", 100)


def test_pacing_switches_to_maintain_near_target():
    assert pacing_decision("approach", 0.2, **PB) == ("maintain", 33)


def test_pacing_holds_maintain_within_hysteresis_gap():
    # 0.5 is above maintain_band but below approach_band -> stay in maintain.
    assert pacing_decision("maintain", 0.5, **PB) == ("maintain", 33)


def test_pacing_reenters_approach_when_drifts_up():
    assert pacing_decision("maintain", 1.5, **PB) == ("approach", 100)


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
