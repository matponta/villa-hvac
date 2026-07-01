"""Tests for the pure PV/energy-aware pre-cool core (F4c-lite)."""
from __future__ import annotations

from custom_components.villa_hvac.supervisor import (
    PRECOOL_BANK,
    PRECOOL_COAST,
    PRECOOL_HOLD,
    cooling_effectiveness,
    energy_precool_decision,
)


# --- cooling_effectiveness ---------------------------------------------------


def test_effectiveness_high_in_cool_dark_hour():
    # morning: T_out just above room, no sun -> gain tiny -> effectiveness ~ k
    eff = cooling_effectiveness(24.0, 25.0, 0.0, a=0.03, b=0.0008, c=0.0, k=1.2)
    assert 1.1 < eff <= 1.2


def test_effectiveness_near_zero_at_hot_sunny_peak():
    # 34°C peak + strong sun overwhelm k -> net ~0 or negative (matches live finding)
    eff = cooling_effectiveness(24.0, 34.0, 900.0, a=0.5, b=0.0008, c=0.0, k=1.2)
    assert eff <= 0.1


# --- energy_precool_decision -------------------------------------------------

# A day shape: cool efficient morning (high eff) rising to an ineffective hot peak.
MORNING_TO_PEAK = [1.1, 1.0, 0.8, 0.5, 0.2, 0.0, -0.3]  # index 0 = now (morning)


def _d(effs, now=0, pv=None, cons=None, **kw):
    return energy_precool_decision(
        effectiveness=effs, now_index=now,
        pv_kwh_remaining=pv, consumption_kwh_remaining=cons, **kw
    )


def test_bank_in_efficient_morning():
    d = _d(MORNING_TO_PEAK, now=0)
    assert d.mode == PRECOOL_BANK


def test_bank_deeper_floor_when_solar_rich():
    rich = _d(MORNING_TO_PEAK, now=0, pv=40.0, cons=30.0)
    poor = _d(MORNING_TO_PEAK, now=0, pv=20.0, cons=30.0)
    assert rich.mode == PRECOOL_BANK and poor.mode == PRECOOL_BANK
    assert rich.solar_rich is True and poor.solar_rich is False
    assert rich.floor < poor.floor  # bank deeper (colder) when solar is free


def test_coast_at_the_hot_peak():
    # now = the ineffective hot peak, but cooler (effective) hours lie ahead in the
    # horizon (evening -> cool night/morning) -> defer to them.
    peak_to_night = [-0.3, 0.0, 0.3, 0.8, 1.1]  # index 0 = now (peak), recovering
    d = _d(peak_to_night, now=0)
    assert d.mode == PRECOOL_COAST


def test_no_coast_when_no_better_hour_ahead():
    # inefficient now AND nothing effective ahead -> HOLD (deferring is pointless;
    # the comfort band still guarantees comfort).
    d = _d([-0.3], now=0)
    assert d.mode == PRECOOL_HOLD


def test_hold_when_borderline():
    # eff_now between eff_min and eff_fraction*eff_peak
    d = _d(MORNING_TO_PEAK, now=3)  # eff_now 0.5; peak from here = 0.5 -> 0.5>=0.6*0.5 -> BANK
    # note: at now=3 the horizon peak IS 0.5 so now is the best -> BANK, not hold.
    assert d.mode == PRECOOL_BANK
    # construct a genuine borderline: now below fraction of a higher later... but
    # effectiveness only falls here. Use a horizon where a better hour is ahead:
    d2 = energy_precool_decision(
        effectiveness=[0.3, 1.0, 0.9], now_index=0,
        pv_kwh_remaining=None, consumption_kwh_remaining=None,
    )  # eff_now 0.3, peak 1.0, 0.3 < 0.6 and 0.3 > eff_min(0.1) -> HOLD
    assert d2.mode == PRECOOL_HOLD


def test_hold_when_nothing_effective_ahead():
    d = _d([0.0, -0.2, -0.5], now=0)  # eff_peak <= eff_min
    assert d.mode == PRECOOL_HOLD
    assert d.floor is None


def test_empty_horizon_holds():
    d = _d([1.0, 0.5], now=5)  # now_index past the end
    assert d.mode == PRECOOL_HOLD


def test_solar_rich_needs_both_values():
    # missing consumption -> cannot be solar_rich -> gentler floor
    d = _d(MORNING_TO_PEAK, now=0, pv=40.0, cons=None)
    assert d.mode == PRECOOL_BANK and d.solar_rich is False


def test_solar_rich_not_true_on_degenerate_zero():
    # end-of-day: both remaining-PV and remaining-consumption ~0. 0 >= 0 must NOT
    # flip solar_rich (that would bank to the deepest floor at night on grid).
    d = _d(MORNING_TO_PEAK, now=0, pv=0.0, cons=0.0)
    assert d.mode == PRECOOL_BANK  # still an efficient hour
    assert d.solar_rich is False   # but NOT solar-rich -> gentler floor_poor
    assert d.floor == 23.0
