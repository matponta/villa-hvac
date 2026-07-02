"""F4c Phase 1: band-center composition contract + comfort floor.

Pins the composition ladder (`compose_center` / COMPOSITION_ORDER) so the unified
planner's Phase-6 replacement is provably behaviour-preserving when off, and
enforces the invariants:

  * final center ∈ [comfort_floor, duty_comfort_max] for a base within that band;
  * no LOWERING feature (PV bank, #9 pre-cool) drives below the floor;
  * no RAISING feature (PV coast, F4b relax) drives above the ceiling;
  * PV bias and #9 pre-cool are MUTUALLY EXCLUSIVE center sources (no double-count);
  * a Via/Notte setback base above the ceiling is NOT clamped down.
"""
from __future__ import annotations

from custom_components.villa_hvac.policies import COMPOSITION_ORDER
from custom_components.villa_hvac.supervisor import compose_center

CEIL = 27.0
FLOOR = 22.0


def _c(**kw):
    args = dict(
        base=24.0, pv_mode=None, pv_floor=None, pv_coast_relax=1.5,
        comfort_enabled=False, comfort_relax=0.0, precool=False, precool_offset=1.5,
        duty_enabled=False, comfort_ceiling=CEIL, comfort_floor=FLOOR,
    )
    args.update(kw)
    return compose_center(**args)


# --- base + single feature ---------------------------------------------------

def test_base_only():
    r = _c()
    assert r.center == 24.0 and r.source == "base" and not r.floored


def test_precool_lowers_within_floor():
    r = _c(precool=True, duty_enabled=True, precool_offset=1.5)
    assert r.center == 22.5 and r.source == "precool" and not r.floored


def test_precool_gated_by_duty():
    # #9 pre-cool only applies when duty is enabled (matches the old ladder).
    assert _c(precool=True, duty_enabled=False).center == 24.0


def test_comfort_relax_raises():
    r = _c(comfort_relax=2.0)                     # 24 + 2 = 26 <= ceiling
    assert r.center == 26.0 and r.source == "comfort_relax"


def test_pv_bank_banks_toward_floor():
    r = _c(pv_mode="bank", pv_floor=23.0)
    assert r.center == 23.0 and r.source == "pv_bank" and not r.floored


def test_pv_coast_raises_and_is_capped_at_ceiling():
    r = _c(pv_mode="coast", pv_coast_relax=5.0)   # 24 + 5 = 29 -> capped 27
    assert r.center == CEIL and r.source == "pv_coast"


# --- comfort FLOOR invariant (the new Phase-1 bound) -------------------------

def test_precool_bounded_below_by_comfort_floor():
    r = _c(precool=True, duty_enabled=True, precool_offset=5.0)  # 24-5=19 < floor
    assert r.center == FLOOR and r.floored


def test_pv_bank_bounded_below_by_comfort_floor():
    r = _c(pv_mode="bank", pv_floor=18.0)         # below the floor
    assert r.center == FLOOR and r.floored


# --- cross-feature invariants ------------------------------------------------

def test_cross_feature_center_stays_in_band():
    """Co-enable everything with a base inside the band -> final center in band."""
    for pv in (None, "bank", "coast"):
        r = _c(
            pv_mode=pv, pv_floor=18.0, pv_coast_relax=5.0, comfort_enabled=True,
            comfort_relax=3.0, precool=True, duty_enabled=True, precool_offset=5.0,
        )
        assert FLOOR <= r.center <= CEIL, (pv, r)


def test_pv_and_precool_are_mutually_exclusive():
    """PV bank present -> the #9 pre-cool branch is NOT also applied (single source,
    never double-counted) — encodes the composition contract."""
    r = _c(pv_mode="bank", pv_floor=23.0, precool=True, duty_enabled=True,
           precool_offset=1.5)
    assert r.center == 23.0 and r.source == "pv_bank"  # 23, not 23-1.5


def test_setback_base_above_ceiling_not_clamped_down():
    """A Via/Notte deep-setback base above the comfort ceiling is the mode's choice,
    NOT a raising 'feature' -> compose_center must not clamp it down. (F4b
    comfort_relax is pre-capped to 0 by the engine when base >= ceiling, so it
    never adds on top of a setback base in the real flow.)"""
    assert _c(base=29.0).center == 29.0
    # a LOWERING feature still applies to a setback base (bounded by the floor).
    assert _c(
        base=29.0, precool=True, duty_enabled=True, precool_offset=1.5
    ).center == 27.5


def test_composition_order_matches_compose_center_branches():
    """COMPOSITION_ORDER (the documented ladder) lists exactly the sources
    compose_center can emit."""
    sources = {row[0] for row in COMPOSITION_ORDER}
    assert sources == {"pv_bank", "pv_coast", "precool", "comfort_relax", "base"}
