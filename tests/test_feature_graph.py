"""Tests for the R4 (Tier-1) feature graph — per-optimizer observability."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.villa_hvac.supervisor import (
    FEATURE_ORDER,
    DutyState,
    HouseState,
    RunPlan,
    ZoneSnapshot,
    build_feature_graph,
    build_plan,
)

T0 = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)
LOOK = timedelta(hours=12)

ALL_ON = {f: True for f in FEATURE_ORDER}
ALL_OFF = {f: False for f in FEATURE_ORDER}


def _leader(zone_id="lr", **kw):
    """A cooling-fancoil leader (satisfies active_cooling_leaders)."""
    opts = dict(
        name=zone_id, climate=f"climate.{zone_id}", emitter="fancoil",
        fancoil_units=((f"fan.{zone_id}", f"switch.{zone_id}"),),
        temp=25.0, enabled=True,
    )
    opts.update(kw)
    return ZoneSnapshot(zone_id=zone_id, **opts)


def _house(zones=(), **kw):
    opts = dict(season="summer", house_mode="Casa", house_setpoint=24.0,
                mode_offset=0.0, outdoor_temp=26.0, consenso_freddo="off")
    opts.update(kw)
    return HouseState(now=T0, zones={z.zone_id: z for z in zones}, **opts)


def _graph(house, *, master_on, enabled, **plan_kw):
    plan = build_plan(house, RunPlan(), {}, DutyState(), [], LOOK)
    plan = replace(plan, **plan_kw)
    return {
        f.feature: f
        for f in build_feature_graph(house, plan, master_on=master_on, enabled=enabled)
    }


# --- ordering + shape --------------------------------------------------------

def test_row_order_matches_feature_order():
    house = _house(zones=[_leader()])
    plan = build_plan(house, RunPlan(), {}, DutyState(), [], LOOK)
    rows = build_feature_graph(house, plan, master_on=True, enabled=ALL_ON)
    assert tuple(r.feature for r in rows) == FEATURE_ORDER


# --- precedence: disabled > supervisor off > own gate ------------------------

def test_disabled_reads_disabled_even_master_off():
    g = _graph(_house(zones=[_leader()]), master_on=False, enabled=ALL_OFF)
    for f in FEATURE_ORDER:
        assert g[f].enabled is False
        assert g[f].active is False
        assert g[f].inert_reason == "disabled"


def test_enabled_but_master_off_reads_supervisor_off():
    g = _graph(_house(zones=[_leader()]), master_on=False, enabled=ALL_ON)
    for f in FEATURE_ORDER:
        assert g[f].enabled is True
        assert g[f].active is False
        assert g[f].inert_reason == "supervisor off"


# --- per-feature active gates (enabled + master on) --------------------------

def test_duty_active_when_resting_and_peak_reason():
    g = _graph(_house(zones=[_leader()]), master_on=True, enabled=ALL_ON,
               in_cooloff=True)
    assert g["duty_cycle"].active is True
    g2 = _graph(_house(zones=[_leader()]), master_on=True, enabled=ALL_ON,
                in_cooloff=False, at_peak=True)
    assert g2["duty_cycle"].active is False
    assert g2["duty_cycle"].inert_reason == "peak: PdC free-runs"


def test_precool_free_cool_shading_night_active():
    house = _house(zones=[_leader()], night_active=True)
    g = _graph(house, master_on=True, enabled=ALL_ON,
               precool=True, free_cool=True, covers_closing=("cover.x",))
    assert g["precool"].active is True
    assert g["free_cool"].active is True
    assert g["shading"].active is True
    assert g["night"].active is True


def test_pv_bias_active_on_bank():
    g = _graph(_house(zones=[_leader()], pv_mode="bank"),
               master_on=True, enabled=ALL_ON)
    assert g["pv_bias"].active is True
    g2 = _graph(_house(zones=[_leader()], pv_mode=None),
                master_on=True, enabled=ALL_ON)
    assert g2["pv_bias"].active is False
    assert g2["pv_bias"].inert_reason == "no PV surplus to bank"


def test_free_air_active_when_enabled():
    g = _graph(_house(zones=[_leader()]), master_on=True, enabled=ALL_ON)
    assert g["free_air"].active is True and g["free_air"].inert_reason is None


def test_unified_planner_active_when_driving():
    g = _graph(_house(zones=[_leader(planner_driven=True)]),
               master_on=True, enabled=ALL_ON)
    assert g["unified_planner"].active is True


# --- wiring: single call site (observability, not behavior) ------------------

def test_build_feature_graph_called_only_from_build_plan_view():
    engine_src = Path(
        "custom_components/villa_hvac/engine.py"
    ).read_text(encoding="utf-8")
    # exactly one CALL (the import line has no trailing "(")
    assert engine_src.count("build_feature_graph(") == 1
