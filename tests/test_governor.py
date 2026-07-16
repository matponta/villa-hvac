"""Living-room Steady Governor tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac.governor import (
    SteadyGovernorController,
    steady_governor_step,
)
from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot, fan_lever

T0 = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def test_pure_law_never_emits_normal_100():
    fan, _, _ = steady_governor_step(
        fan=70, floor=20, context="MARGINAL_CALL", error=0.8,
        duty=1.0, strokes_h=0.0, kitchen_fast_rise=True,
        stable_evaluations=0,
    )
    assert fan == 70


def test_shared_call_steps_down_only_after_two_stable_evaluations():
    fan, _, stable = steady_governor_step(
        fan=40, floor=20, context="SHARED_CALL", error=0.1,
        duty=0.7, strokes_h=1.0, kitchen_fast_rise=False,
        stable_evaluations=0,
    )
    assert fan == 40 and stable == 1
    fan, _, stable = steady_governor_step(
        fan=fan, floor=20, context="SHARED_CALL", error=0.1,
        duty=0.7, strokes_h=1.0, kitchen_fast_rise=False,
        stable_evaluations=stable,
    )
    assert fan == 30 and stable == 0


def _house(
    now=T0, *, steady=True, selected=True, mode="Casa", temp=24.1,
    target=24.0, demand=True, kitchen=25.0,
):
    living = ZoneSnapshot(
        zone_id="living_room", name="Salotto",
        climate="climate.salotto_termostato_2", emitter="fancoil",
        temp=temp, demand=demand, resolved_center=target, fan_min=20,
        fan_pct=100,
        fancoil_units=(
            ("fan.fancoil_salotto", "switch.fancoil_salotto_manuale"),
            ("fan.fancoil_cucina", "switch.fancoil_cucina_manuale"),
        ),
    )
    kitchen_zone = ZoneSnapshot(
        zone_id="kitchen", name="Cucina", climate=None,
        emitter="fancoil", demand=demand,
    )
    return HouseState(
        now=now, zones={"living_room": living, "kitchen": kitchen_zone},
        season="summer", house_mode=mode,
        steady_pacing_enabled=steady, paced_living_room=selected,
        kitchen_ep_temp=kitchen, kitchen_ep_fresh=True,
    )


def test_shadow_computes_candidate_without_writing():
    c = SteadyGovernorController()
    out = c(_house(selected=False))
    assert out == {}
    assert c.view["state"] == "SHADOW"
    assert 20 <= c.view["proposed_fan"] <= 70


def test_selected_living_room_writes_honest_target_and_equal_fans():
    c = SteadyGovernorController()
    out = c(_house())
    assert out["temperature:climate.salotto_termostato_2"] == 24.0
    assert out[fan_lever("fan.fancoil_salotto")] == 40
    assert out[fan_lever("fan.fancoil_cucina")] == 40
    assert all(value != 0 for key, value in out.items() if key.startswith("fan:"))


def test_notte_hands_back_manual_and_leaves_both_fans_alive():
    c = SteadyGovernorController()
    c(_house())
    out = c(_house(now=T0 + timedelta(minutes=1), mode="Notte"))
    assert out["switch:switch.fancoil_salotto_manuale"] == "off"
    assert out["switch:switch.fancoil_cucina_manuale"] == "off"
    assert out[fan_lever("fan.fancoil_salotto")] > 0
    assert out[fan_lever("fan.fancoil_cucina")] > 0


def test_paced_off_hands_back_manual_and_leaves_both_fans_alive():
    # Regression: turning paced_living_room OFF while steady_pacing stays ON and
    # the zone is still eligible must hand the fans back to AUTO alive (documented
    # rollback) — not strand Salotto+Cucina manuale ON with an empty emit.
    c = SteadyGovernorController()
    c(_house())                                              # actuating
    out = c(_house(now=T0 + timedelta(minutes=1), selected=False))
    assert out, "un-actuate must emit a hand-back, not an empty dict"
    assert out["switch:switch.fancoil_salotto_manuale"] == "off"
    assert out["switch:switch.fancoil_cucina_manuale"] == "off"
    assert out[fan_lever("fan.fancoil_salotto")] > 0
    assert out[fan_lever("fan.fancoil_cucina")] > 0


def test_large_error_escalates_to_native_auto():
    c = SteadyGovernorController()
    c(_house())
    out = c(_house(now=T0 + timedelta(minutes=1), temp=25.1))
    assert c.view["state"] == "ESCALATED"
    assert out["switch:switch.fancoil_salotto_manuale"] == "off"
