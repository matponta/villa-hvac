"""Tests for the pure Supervisor write-arbiter core (Phase A).

Covers the manual-override re-assert state machine (the #1 robustness risk:
distinguishing a dropped KNX telegram from a real hand change) plus the
priority merge and the tolerance compare.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.villa_hvac.supervisor import (
    DEFAULT_OVERRIDE_BACKOFF,
    LeverState,
    merge_desired,
    reconcile,
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
