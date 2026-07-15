"""Rack hardware guard tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac.const import (
    RACK_GUARD_ENGAGE,
    RACK_GUARD_NO_RESPONSE,
    RACK_GUARD_RELEASE,
)
from custom_components.villa_hvac.rack import (
    RackGuardController,
    RackGuardState,
    rack_guard_step,
)
from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot, fan_lever

T0 = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def test_rack_guard_engage_release_and_emergency_hysteresis():
    state, action = rack_guard_step(RackGuardState(), 28.5, 28.0, T0)
    assert action is None and state.above_since == T0
    state, action = rack_guard_step(state, 28.5, 28.0, T0 + RACK_GUARD_ENGAGE)
    assert action == "engage" and state.active

    state, action = rack_guard_step(state, 30.1, 28.0, T0 + timedelta(minutes=4))
    assert not state.escalated
    state, action = rack_guard_step(state, 30.1, 28.0, T0 + timedelta(minutes=7))
    assert action == "escalate" and state.escalated

    state, action = rack_guard_step(state, 26.9, 28.0, T0 + timedelta(minutes=8))
    assert state.active
    state, action = rack_guard_step(
        state, 26.9, 28.0, T0 + timedelta(minutes=8) + RACK_GUARD_RELEASE
    )
    assert action == "release" and not state.active


def test_rack_guard_escalates_after_no_response():
    state = RackGuardState(
        active=True, activated_at=T0, activation_temp=28.5
    )
    state, action = rack_guard_step(
        state, 28.3, 28.0, T0 + RACK_GUARD_NO_RESPONSE
    )
    assert action == "escalate" and state.escalated


def _house(now=T0, *, rack_temp=28.5, p1_temp=27.0, enabled=True, paused=False):
    return HouseState(
        now=now,
        zones={
            "rack": ZoneSnapshot(
                zone_id="rack", name="Rack", climate=None, emitter="fancoil",
                temp=rack_temp,
            ),
            "stairs_p1": ZoneSnapshot(
                zone_id="stairs_p1", name="P1",
                climate="climate.pianerottolo_p1_termostato_2", emitter="fancoil",
                temp=p1_temp, enabled=enabled, paused=paused,
            ),
        },
        rack_guard_enabled=True,
        rack_temp_threshold=28.0,
        season="summer",
        house_mode="Casa",
        house_setpoint=24.0,
        mode_offset=0.0,
    )


def test_controller_commands_67_then_100_and_restores():
    c = RackGuardController()
    assert c(_house()) == {}
    out = c(_house(T0 + RACK_GUARD_ENGAGE))
    rack_fan = fan_lever("fan.fancoil_locale_rack")
    assert out[rack_fan] == 67
    assert c.failsafe_setpoints() == {
        "climate.pianerottolo_p1_termostato_2": 24.0
    }


def test_controller_yields_when_p1_paused():
    c = RackGuardController()
    c(_house())
    c(_house(T0 + RACK_GUARD_ENGAGE))
    out = c(_house(T0 + timedelta(minutes=4), paused=True))
    assert out[fan_lever("fan.fancoil_locale_rack")] == 67


def test_yielded_critical_rack_alerts_once_and_rearms_after_recovery():
    c = RackGuardController()
    c(_house(T0, rack_temp=30.2, paused=True))
    c(_house(T0 + timedelta(minutes=30), rack_temp=30.2, paused=True))
    assert c._alert_sent
    assert "sospesa" in c.alert_reason
    c(_house(T0 + timedelta(minutes=31), rack_temp=26.5, paused=True))
    assert not c._alert_sent
