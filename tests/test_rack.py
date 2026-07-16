"""Rack hardware guard tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac.const import (
    RACK_GUARD_ENGAGE,
    RACK_GUARD_NO_RESPONSE,
    RACK_GUARD_RELEASE,
)
from custom_components.villa_hvac.rack import (
    P1GuardController,
    RackGuardController,
    RackGuardState,
    rack_guard_step,
)
from custom_components.villa_hvac.supervisor import (
    BLOCCO_LEVER,
    BLOCCO_RELEASE,
    HouseState,
    ZoneSnapshot,
    fan_lever,
    switch_lever,
    temperature_lever,
)

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


def test_controller_hands_back_on_natural_cooldown():
    # Regression: an ACTIVE guard whose rack cools below release for the full
    # window while still eligible must hand back (manuale OFF, fan alive, setpoint
    # restored) — not silently return {} and strand the shared fan in manual.
    c = RackGuardController()
    c(_house())                          # above_since
    c(_house(T0 + RACK_GUARD_ENGAGE))    # engage: manuale ON, fan 67, P1 nudged
    c(_house(T0 + timedelta(minutes=5), rack_temp=26.5))          # below_since
    out = c(_house(T0 + timedelta(minutes=5) + RACK_GUARD_RELEASE, rack_temp=26.5))
    assert out, "cool-down must emit a hand-back, not an empty dict"
    assert out[switch_lever("switch.fancoil_locale_rack_manuale")] == "off"
    assert out[fan_lever("fan.fancoil_locale_rack")] == 67          # fan left alive
    assert out[temperature_lever("climate.pianerottolo_p1_termostato_2")] == 24.0
    assert c._snapshot is None and not c.state.active


def test_yielded_critical_rack_alerts_once_and_rearms_after_recovery():
    c = RackGuardController()
    c(_house(T0, rack_temp=30.2, paused=True))
    c(_house(T0 + timedelta(minutes=30), rack_temp=30.2, paused=True))
    assert c._alert_sent
    assert "sospesa" in c.alert_reason
    c(_house(T0 + timedelta(minutes=31), rack_temp=26.5, paused=True))
    assert not c._alert_sent


# --- P1 "both fans" secondary trigger (P1GuardController) --------------------

def _p1house(
    now=T0, *, p1_temp=28.0, office_temp=25.0, enabled=True, paused=False,
    guard_enabled=True, house_setpoint=24.0,
):
    return HouseState(
        now=now,
        zones={
            "stairs_p1": ZoneSnapshot(
                zone_id="stairs_p1", name="P1",
                climate="climate.pianerottolo_p1_termostato_2", emitter="fancoil",
                temp=p1_temp, enabled=enabled, paused=paused,
            ),
            "office": ZoneSnapshot(
                zone_id="office", name="Office",
                climate="climate.studio_termostato_2", emitter="fancoil",
                temp=office_temp, enabled=True,
            ),
            "rack": ZoneSnapshot(
                zone_id="rack", name="Rack", climate=None, emitter="fancoil",
                temp=26.0,
            ),
        },
        p1_guard_enabled=guard_enabled,
        p1_guard_threshold=27.0,
        season="summer",
        house_mode="Casa",
        house_setpoint=house_setpoint,
        mode_offset=0.0,
    )


def test_p1_guard_engages_both_fans_and_nudges_both():
    c = P1GuardController()
    assert c(_p1house()) == {}                        # above_since only
    out = c(_p1house(T0 + RACK_GUARD_ENGAGE))          # engage
    assert out[switch_lever("switch.fancoil_locale_rack_manuale")] == "on"
    assert out[fan_lever("fan.fancoil_locale_rack")] == 67
    assert out[switch_lever("switch.fancoil_studio_pianerottolo_p1_manuale")] == "on"
    assert out[fan_lever("fan.fancoil_studio_pianerottolo_p1")] == 67
    # both thermostats nudged down; office is never driven above its own base (24)
    assert out[temperature_lever("climate.pianerottolo_p1_termostato_2")] <= 24.0
    assert out[temperature_lever("climate.studio_termostato_2")] <= 24.0
    assert out[BLOCCO_LEVER] == BLOCCO_RELEASE


def test_p1_guard_nudge_never_exceeds_base_with_cold_setpoint():
    # Regression (review D): with a cold house base (< 20) the nudge must stay
    # ≤ base — never driven WARMER than the zone's own target by the 20° floor.
    c = P1GuardController()
    # base = house_setpoint 17 + Casa offset 0 = 17 (< the 20° floor)
    c(_p1house(house_setpoint=17.0, office_temp=18.0))                 # above_since
    out = c(_p1house(T0 + RACK_GUARD_ENGAGE, house_setpoint=17.0, office_temp=18.0))
    assert out[temperature_lever("climate.pianerottolo_p1_termostato_2")] <= 17.0
    assert out[temperature_lever("climate.studio_termostato_2")] <= 17.0


def test_p1_guard_hands_back_both_on_cooldown():
    c = P1GuardController()
    c(_p1house())
    c(_p1house(T0 + RACK_GUARD_ENGAGE))
    c(_p1house(T0 + timedelta(minutes=5), p1_temp=25.5))
    out = c(_p1house(T0 + timedelta(minutes=5) + RACK_GUARD_RELEASE, p1_temp=25.5))
    assert out, "cool-down must hand back, not emit nothing"
    assert out[switch_lever("switch.fancoil_locale_rack_manuale")] == "off"
    assert out[switch_lever("switch.fancoil_studio_pianerottolo_p1_manuale")] == "off"
    assert out[fan_lever("fan.fancoil_locale_rack")] == 67          # fans left alive
    assert out[fan_lever("fan.fancoil_studio_pianerottolo_p1")] == 67
    assert out[temperature_lever("climate.pianerottolo_p1_termostato_2")] == 24.0
    assert out[temperature_lever("climate.studio_termostato_2")] == 24.0
    assert c._snap_p1 is None and c._snap_office is None


def test_p1_guard_never_emits_fan_zero_and_failsafe_restores_both():
    c = P1GuardController()
    c(_p1house())
    out = c(_p1house(T0 + RACK_GUARD_ENGAGE))
    assert all(v != 0 for k, v in out.items() if k.startswith("fan:"))
    assert c.failsafe_setpoints() == {
        "climate.pianerottolo_p1_termostato_2": 24.0,
        "climate.studio_termostato_2": 24.0,
    }


def test_p1_guard_yields_when_disabled_or_p1_paused():
    c = P1GuardController()
    assert c(_p1house(guard_enabled=False)) == {}     # opt-in off -> nothing
    c2 = P1GuardController()
    c2(_p1house())
    c2(_p1house(T0 + RACK_GUARD_ENGAGE))              # active
    out = c2(_p1house(T0 + timedelta(minutes=4), paused=True))  # P1 paused -> release
    assert out[switch_lever("switch.fancoil_locale_rack_manuale")] == "off"
    assert out[switch_lever("switch.fancoil_studio_pianerottolo_p1_manuale")] == "off"
