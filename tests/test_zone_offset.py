"""Tests for #2 per-zone comfort offset (per-room trim on the house base)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac import controller
from custom_components.villa_hvac.policies import house_mode_policy, precool_policy
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    resolve_center,
    temperature_lever,
)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
MAXAGE = timedelta(hours=2)


def _leader(zid="office", **kw):
    opts = dict(
        name=zid, climate=f"climate.{zid}", emitter="fancoil",
        fancoil_units=((f"fan.{zid}", f"switch.{zid}"),), temp=25.0, enabled=True,
    )
    opts.update(kw)
    return ZoneSnapshot(zone_id=zid, **opts)


def _house(zones, **kw):
    opts = dict(
        now=T0, season="summer", house_setpoint=24.0, mode_offset=0.0,
        auto_setback=True, house_mode="Casa",
        duty_comfort_max=27.0, comfort_floor=22.0,
    )
    opts.update(kw)
    return HouseState(zones={z.zone_id: z for z in zones}, **opts)


# --- helper clamp/default ----------------------------------------------------

def test_helper_clamps_and_defaults(monkeypatch):
    monkeypatch.setattr(controller, "_number_value", lambda h, e, s: -5.0)
    assert controller.setpoint_offset(None, None, "office") == -3.0  # clamp low
    monkeypatch.setattr(controller, "_number_value", lambda h, e, s: 9.0)
    assert controller.setpoint_offset(None, None, "office") == 3.0   # clamp high
    monkeypatch.setattr(controller, "_number_value", lambda h, e, s: None)
    assert controller.setpoint_offset(None, None, "office") == 0.0   # unset -> 0
    monkeypatch.setattr(controller, "_number_value", lambda h, e, s: -1.5)
    assert controller.setpoint_offset(None, None, "office") == -1.5


# --- application at the reactive sites ---------------------------------------

def test_resolve_center_base_includes_offset():
    z = _leader(setpoint_offset=-1.0)
    res = resolve_center(z, _house([z]), max_age=MAXAGE)
    assert res.base == 23.0             # 24 + 0 + (-1)
    assert res.center == 23.0           # no feature active -> center == base


def test_zero_offset_is_byte_identical():
    z0 = _leader(setpoint_offset=0.0)
    assert resolve_center(z0, _house([z0]), max_age=MAXAGE).base == 24.0


def test_house_mode_policy_setpoint_includes_offset():
    z = _leader(setpoint_offset=0.5)
    out = house_mode_policy(_house([z]))
    assert out[temperature_lever(z.climate)] == 24.5  # 24 + 0 + 0.5


def test_precool_policy_target_includes_offset():
    z = _leader(setpoint_offset=-0.5)
    st = _house([z], duty_enabled=True, precool=True, precool_offset=1.5)
    out = precool_policy(st)
    assert out[temperature_lever(z.climate)] == 22.0  # 24 + 0 - 1.5 - 0.5


def test_offset_survives_mode_offset_stacking():
    # A +5 summer-Via mode offset plus a -1 room trim -> 24 + 5 - 1 = 28.
    z = _leader(setpoint_offset=-1.0)
    res = resolve_center(z, _house([z], mode_offset=5.0, duty_comfort_max=30.0),
                         max_age=MAXAGE)
    assert res.base == 28.0
