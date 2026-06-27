"""Unit tests for the pure preset policies (Phase A3)."""
from __future__ import annotations

from datetime import datetime

from custom_components.villa_hvac.const import PRESET_BUILDING_PROTECTION
from custom_components.villa_hvac.policies import (
    PRESET_POLICIES,
    disabled_zones_policy,
    house_mode_policy,
    window_pause_policy,
)
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    merge_desired,
    preset_lever,
    temperature_lever,
)

T0 = datetime(2026, 6, 27, 12, 0, 0)


def _zone(zid, climate="climate.x", emitter="fancoil", enabled=True, paused=False):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=climate, emitter=emitter,
        enabled=enabled, paused=paused,
    )


def _state(zones, *, mode="Casa", auto=True, setpoint=24.0, offset=0.0):
    return HouseState(
        now=T0,
        zones={z.zone_id: z for z in zones},
        house_mode=mode,
        auto_setback=auto,
        house_setpoint=setpoint,
        mode_offset=offset,
    )


# --- disabled (#10) ----------------------------------------------------------

def test_disabled_zone_forced_to_building_protection():
    z = _zone("a", climate="climate.a", enabled=False)
    out = disabled_zones_policy(_state([z]))
    assert out == {preset_lever("climate.a"): PRESET_BUILDING_PROTECTION}


def test_enabled_zone_not_touched_by_disabled_policy():
    assert disabled_zones_policy(_state([_zone("a", enabled=True)])) == {}


# --- window pause (#4) -------------------------------------------------------

def test_paused_zone_forced_to_building_protection():
    z = _zone("a", climate="climate.a", paused=True)
    out = window_pause_policy(_state([z]))
    assert out == {preset_lever("climate.a"): PRESET_BUILDING_PROTECTION}


def test_window_policy_respects_auto_setback_off():
    z = _zone("a", paused=True)
    assert window_pause_policy(_state([z], auto=False)) == {}


# --- house mode (#2a) --------------------------------------------------------

def test_house_mode_drives_preset_and_setpoint():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Casa", setpoint=24.0, offset=0.0))
    assert out[preset_lever("climate.a")] == "comfort"
    assert out[temperature_lever("climate.a")] == 24.0


def test_house_mode_applies_offset():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Via", setpoint=24.0, offset=5.0))
    assert out[preset_lever("climate.a")] == "standby"
    assert out[temperature_lever("climate.a")] == 29.0


def test_house_mode_skips_disabled_paused_and_noncontrollable():
    zones = [
        _zone("dis", climate="climate.dis", enabled=False),
        _zone("pause", climate="climate.pause", paused=True),
        _zone("split", climate="climate.split", emitter="split_ac"),
        _zone("nocl", climate=None),
        _zone("ok", climate="climate.ok"),
    ]
    out = house_mode_policy(_state(zones))
    assert preset_lever("climate.ok") in out
    for skipped in ("climate.dis", "climate.pause", "climate.split"):
        assert preset_lever(skipped) not in out


def test_house_mode_vacation_is_bp_with_no_setpoint():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Vacanza", offset=None))
    assert out[preset_lever("climate.a")] == PRESET_BUILDING_PROTECTION
    assert temperature_lever("climate.a") not in out  # frost-fixed, no setpoint


def test_house_mode_noop_when_auto_setback_off_or_unknown_mode():
    z = _zone("a", climate="climate.a")
    assert house_mode_policy(_state([z], auto=False)) == {}
    assert house_mode_policy(_state([z], mode="???")) == {}


# --- merged stack: priority disabled > window > house_mode -------------------

def test_merged_priority_overrides():
    zones = [
        _zone("dis", climate="climate.dis", enabled=False),
        _zone("pause", climate="climate.pause", paused=True),
        _zone("ok", climate="climate.ok"),
    ]
    state = _state(zones, mode="Casa", offset=0.0, setpoint=24.0)
    merged = merge_desired([p(state) for p in PRESET_POLICIES])
    assert merged[preset_lever("climate.dis")] == PRESET_BUILDING_PROTECTION
    assert merged[preset_lever("climate.pause")] == PRESET_BUILDING_PROTECTION
    assert merged[preset_lever("climate.ok")] == "comfort"
    # disabled/paused zones carry no setpoint (never push temp onto a BP zone)
    assert temperature_lever("climate.dis") not in merged
    assert temperature_lever("climate.pause") not in merged
    assert merged[temperature_lever("climate.ok")] == 24.0
