"""Supervisor core — the write-arbiter that runs the house as one organism.

Phase A. Today's controllers (#2/#4/#2b/#2c) each call `climate.*` directly and
race over the same levers. The target is a single Supervisor that, each cycle,
builds one house-state model, runs a priority-ordered policy stack to compute the
*desired* state of every lever, and writes each lever once, idempotently.

This module is the heart of that: the **reconcile state machine** + the priority
**merge** of policy outputs. It is intentionally PURE (no Home Assistant imports)
so the control discipline is fully unit-testable — especially the
manual-override detection, the #1 robustness risk on a flaky KNX bus:

    A single `current != last-written` read is ambiguous. KNX drops telegrams
    (the salotto write loss) and lags attributes (AUTO fan % bounces in
    sub-second triplets), which look identical to a hand change. So we never
    declare "manual" on one read: after writing X we expect X within tolerance;
    if it diverges we RE-ASSERT for N cycles; only divergence that survives the
    re-asserts concedes to manual (back off for a while). A dropped telegram
    converges on re-assert and never trips the override.

The HA wiring (state-model builder, service calls, enable switches, fail-safe)
lives in later increments; nothing here imports homeassistant.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

# --- Tunable defaults (move to the options flow later) -----------------------
# |current - desired| <= this counts as "matched" (°C, for setpoints). Presets
# and switch states compare by string equality regardless of this.
DEFAULT_SETPOINT_TOLERANCE = 0.3
# Re-write a diverged lever for this many cycles before conceding to manual.
DEFAULT_MAX_REASSERTS = 3
# Once conceded, leave the lever alone (manual wins) for this long.
DEFAULT_OVERRIDE_BACKOFF = timedelta(hours=2)

# State strings that mean "don't conclude anything this cycle".
TRANSIENT_STATES: tuple[str | None, ...] = ("unavailable", "unknown", None, "")


@dataclass(frozen=True)
class LeverState:
    """Per-lever bookkeeping for the reconcile state machine.

    `written` is the value we last asserted; `attempts` counts consecutive
    re-asserts while the read stays diverged; `override_until` is set when we
    have conceded the lever to a manual change.
    """

    written: str | None = None
    attempts: int = 0
    override_until: datetime | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of one reconcile: the new lever state + an optional write.

    `write` is the value to send this cycle (None = do nothing). `note` is a
    diagnostic label for logging/tests, never a control input.
    """

    state: LeverState
    write: str | None = None
    note: str = ""


def values_match(
    current: str | float | None,
    desired: str | float | None,
    tolerance: float,
) -> bool:
    """True if a read equals a target. Numbers compare within `tolerance`
    (setpoints); everything else compares as strings (presets, on/off)."""
    if current is None or desired is None:
        return current == desired
    try:
        return abs(float(current) - float(desired)) <= tolerance
    except (TypeError, ValueError):
        return str(current) == str(desired)


def reconcile(
    desired: str | float | None,
    current: str | float | None,
    state: LeverState,
    now: datetime,
    *,
    tolerance: float = DEFAULT_SETPOINT_TOLERANCE,
    max_reasserts: int = DEFAULT_MAX_REASSERTS,
    backoff: timedelta = DEFAULT_OVERRIDE_BACKOFF,
) -> ReconcileResult:
    """Decide what (if anything) to write for one lever this cycle.

    `desired` is the merged policy opinion (None = no opinion → release control).
    `current` is the live read (may be a transient `unavailable`/`unknown`).
    Returns the next `LeverState` and an optional write. Pure: same inputs →
    same output, so the whole discipline is unit-testable.
    """
    # 1. Honor an active manual-override backoff: hands off the lever.
    if state.override_until is not None and now < state.override_until:
        return ReconcileResult(state=state, note="manual-hold")
    # Backoff expired → forget history and reconcile fresh.
    if state.override_until is not None:
        state = LeverState()

    # 2. No opinion → release: write nothing, drop any tracking.
    if desired is None:
        return ReconcileResult(state=LeverState(), note="released")

    # 3. Transient read → wait; never conclude "manual" from unavailable/unknown.
    if current is None or (isinstance(current, str) and current in TRANSIENT_STATES):
        return ReconcileResult(state=state, note="transient")

    # 4. Already where we want it (set by us OR by anyone) → satisfied.
    if values_match(current, desired, tolerance):
        return ReconcileResult(
            state=replace(state, written=str(desired), attempts=0), note="satisfied"
        )

    # 5. Diverged. First time we want this value (or it changed) → write it.
    if state.written is None or not values_match(state.written, desired, tolerance):
        return ReconcileResult(
            state=replace(state, written=str(desired), attempts=1),
            write=str(desired),
            note="write",
        )

    # 6. We already asserted `desired` but the read still diverges → dropped
    #    telegram or a hand change. Re-assert up to the limit before judging.
    if state.attempts < max_reasserts:
        return ReconcileResult(
            state=replace(state, attempts=state.attempts + 1),
            write=str(desired),
            note="reassert",
        )

    # 7. Divergence survived every re-assert → treat as a manual change and
    #    concede the lever for the backoff window.
    return ReconcileResult(
        state=LeverState(override_until=now + backoff), note="override"
    )


def merge_desired(
    ordered_outputs: list[dict[str, str | float | None]],
) -> dict[str, str | float | None]:
    """Merge per-policy desired-lever maps; highest priority wins per lever.

    `ordered_outputs` is the policy stack's outputs in HIGH→LOW priority order.
    The first policy to express an opinion on a lever owns it; lower policies
    cannot override. A present key with value None is an explicit "release"
    opinion and still wins over lower policies (e.g. a guardrail freeing a
    lever beats an optimizer wanting to drive it).
    """
    merged: dict[str, str | float | None] = {}
    for output in ordered_outputs:
        for lever, value in output.items():
            if lever not in merged:
                merged[lever] = value
    return merged
