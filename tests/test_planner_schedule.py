"""F4c Phase 5: the unified band-center REFERENCE schedule (plan_center_schedule).

Pure, PLAN-ONLY: it emits a per-leader hourly center reference + an advisory house
intent, composing the shipping cores (schedule_precool / energy_precool_decision /
run_rest_durations / return_lead_time). It drives NOTHING here — Phase 6 wires it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.villa_hvac.supervisor import (
    HouseState,
    RoomParams,
    ZoneSnapshot,
    plan_center_schedule,
)

NOW = datetime(2026, 7, 2, 6, 0, 0)
LOOK = timedelta(hours=12)
DT = 60  # hourly steps for a legible test


def _leader(zid="lr", *, temp=25.0, eligible=False):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter="fancoil",
        temp=temp, fancoil_units=((f"fan.{zid}", f"switch.{zid}_man"),),
        model_planner_eligible=eligible,
    )


def _state(zones, *, setpoint=24.0, offset=0.0, outdoor=24.0, solar=0.0,
           comfort_floor=22.0, duty_comfort_max=27.0, night=False):
    return HouseState(
        now=NOW, zones={z.zone_id: z for z in zones},
        house_setpoint=setpoint, mode_offset=offset,
        band_width=1.5, band_slam=0.75,
        comfort_floor=comfort_floor, duty_comfort_max=duty_comfort_max,
        outdoor_temp=outdoor, solar=solar, season="summer", night_active=night,
    )


def _params(k=1.5):
    return RoomParams(a=0.03, b=0.0008, c=0.0, k=k, pulldown=0.3, fan_min=0)


def _flat_forecast(temp):
    return [(NOW + timedelta(hours=h), temp) for h in range(13)]


def _flat_solar(v):
    return [v] * 13


# --- base only ---------------------------------------------------------------

def test_base_only_reference_is_flat_at_center():
    st = _state([_leader(temp=24.0)], outdoor=24.0)  # at center -> no breach/precool
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    zs = sched.zones["lr"]
    assert zs.points and all(p.center == 24.0 and p.source == "base" for p in zs.points)
    assert zs.precool_depth == 0.0
    assert sched.house_blocco == "off"  # the reference NEVER blocks


def test_no_setpoint_yields_empty_schedule():
    st = _state([_leader()])
    st = HouseState(now=NOW, zones=st.zones, house_setpoint=None, mode_offset=None)
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    assert sched.zones == {} and sched.house_blocco == "off"


# --- #9 pre-cool overlay -----------------------------------------------------

def test_precool_lowers_center_before_peak_bounded_by_floor():
    # a hot afternoon peak -> the base trajectory breaches -> pre-cool schedules a
    # depth -> the reference dips below base before the peak, never below the floor.
    fc = [(NOW + timedelta(hours=h), 24.0 + (2.0 * h)) for h in range(13)]  # ramp to 48
    st = _state([_leader(temp=26.0)], outdoor=24.0)
    sched = plan_center_schedule(
        st, {"lr": _params(k=1.0)}, fc, _flat_solar(300.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    zs = sched.zones["lr"]
    centers = [p.center for p in zs.points]
    assert min(centers) < 24.0                    # pre-cool banked below base
    assert min(centers) >= 22.0                    # never below the comfort floor
    assert zs.precool_depth > 0 and any(p.source == "precool" for p in zs.points)


# --- PV bias -----------------------------------------------------------------

def test_pv_bank_lowers_center_to_floor_in_efficient_hours():
    # cool + low-sun hours -> effective -> BANK to the solar-rich floor (22).
    st = _state([_leader(temp=24.0)], outdoor=20.0, solar=100.0)
    sched = plan_center_schedule(
        st, {"lr": _params(k=2.0)}, _flat_forecast(20.0), _flat_solar(100.0),
        lookahead=LOOK, max_precool_depth=3.0,
        pv_active=True, pv_kwh_remaining=50.0, consumption_kwh_remaining=10.0,
        pv_floor_rich=22.0, pv_floor_poor=23.0, dt_min=DT,
    )
    zs = sched.zones["lr"]
    assert any(p.source == "pv_bank" and p.center == 22.0 for p in zs.points)


def test_pv_inactive_leaves_base():
    st = _state([_leader(temp=24.0)], outdoor=20.0, solar=100.0)
    sched = plan_center_schedule(
        st, {"lr": _params(k=2.0)}, _flat_forecast(20.0), _flat_solar(100.0),
        lookahead=LOOK, max_precool_depth=3.0, pv_active=False, dt_min=DT,
    )
    assert all(p.source == "base" for p in sched.zones["lr"].points)


# --- lookup + staleness ------------------------------------------------------

def test_at_step_interpolates():
    st = _state([_leader(temp=24.0)])  # at center -> flat base reference
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    assert sched.at("lr", NOW) == 24.0
    assert sched.at("lr", NOW + timedelta(minutes=90)) == 24.0  # last point <= 90m
    assert sched.at("missing", NOW) is None


def test_is_stale():
    st = _state([_leader()])
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    assert sched.is_stale(NOW + timedelta(minutes=40), timedelta(minutes=30)) is True
    assert sched.is_stale(NOW + timedelta(minutes=20), timedelta(minutes=30)) is False


# --- composed house intent + eligibility -------------------------------------

def test_house_run_rest_advisory_present():
    st = _state([_leader(temp=26.0)], outdoor=30.0, solar=400.0)
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(30.0), _flat_solar(400.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    # a warm house with a gaining room -> run/rest estimates exist (advisory).
    assert sched.house_rest is not None


def test_return_lead_computed_when_eta_armed():
    st = _state([_leader(temp=28.0)], outdoor=30.0)
    sched = plan_center_schedule(
        st, {"lr": _params()}, _flat_forecast(30.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0,
        eta=NOW + timedelta(hours=4), dt_min=DT,
    )
    assert sched.return_lead is not None and sched.return_lead > timedelta(0)


def test_eligibility_flag_reflects_model():
    st = _state([_leader("easy", eligible=True), _leader("hard", eligible=False)])
    sched = plan_center_schedule(
        st, {"easy": _params(), "hard": _params()},
        _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    assert sched.zones["easy"].eligible is True
    assert sched.zones["hard"].eligible is False   # stays ADVISORY


def test_bedroom_at_night_excluded():
    bed = _leader("bed")
    bed = ZoneSnapshot(
        zone_id="bed", name="bed", climate="climate.bed", emitter="fancoil",
        temp=25.0, bedroom=True, fancoil_units=(("fan.bed", "switch.bed_man"),),
    )
    st = _state([bed], night=True)
    sched = plan_center_schedule(
        st, {"bed": _params()}, _flat_forecast(24.0), _flat_solar(0.0),
        lookahead=LOOK, max_precool_depth=3.0, dt_min=DT,
    )
    assert "bed" not in sched.zones  # camere silenziose owns it at night
