"""Pure write-arbiter (C2 split of supervisor): the reconcile state machine,
the priority merge, and the lever-key helpers. The #1 robustness discipline —
distinguishing a dropped KNX telegram from a real hand change — lives here."""
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


# Consenso BLOCCO switch states (verify polarity live before actuating; observed
# 2026-06-27: OFF = released/cooling allowed).
BLOCCO_BLOCK = "on"      # block the villa cooling call to the PdC

BLOCCO_RELEASE = "off"   # allow the villa to cool



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
    allow_override: bool = True,
) -> ReconcileResult:
    """Decide what (if anything) to write for one lever this cycle.

    `desired` is the merged policy opinion (None = no opinion → release control).
    `current` is the live read (may be a transient `unavailable`/`unknown`).
    Returns the next `LeverState` and an optional write. Pure: same inputs →
    same output, so the whole discipline is unit-testable.

    `allow_override` gates the manual-override concession (step 7). Conceding to
    a human is right for a comfort setpoint, but WRONG for the safety-critical
    global cooling block: its fail direction is RELEASE, and a few dropped
    telegrams must never latch the villa into a multi-hour no-cooling state. For
    such a lever pass `allow_override=False` — it re-asserts the desired value
    indefinitely instead of conceding.
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

    # 7. Divergence survived every re-assert. For a normal lever, treat it as a
    #    manual change and concede for the backoff window. For a no-concede lever
    #    (the global block), keep re-asserting the safe value indefinitely — a
    #    lossy bus must never be able to latch it away from us.
    if not allow_override:
        return ReconcileResult(
            state=replace(state, attempts=max_reasserts),
            write=str(desired),
            note="reassert-hold",
        )
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



# --- Lever-key helpers -------------------------------------------------------
# A lever is addressed by "<kind>:<entity>"; the engine reads/writes by kind.
# The global cooling block has no entity in its key.

BLOCCO_LEVER = "blocco"



def preset_lever(climate_entity: str) -> str:
    return f"preset:{climate_entity}"



def temperature_lever(climate_entity: str) -> str:
    return f"temperature:{climate_entity}"



def fan_lever(fan_entity: str) -> str:
    return f"fan:{fan_entity}"



def cover_lever(cover_entity: str) -> str:
    return f"cover:{cover_entity}"



def switch_lever(switch_entity: str) -> str:
    return f"switch:{switch_entity}"



# Split-AC (standard `climate`) levers. Unlike the KNX thermostats these expose
# a real on/off + operation mode as the entity STATE and a fan_mode STRING enum
# (not a fan.* percentage), so they get their own lever kinds. `temperature:` is
# reused verbatim (SERVICE_SET_TEMPERATURE works on any climate). `hvac_mode`
# carries off/cool/dry/fan_only/heat/auto; `set_hvac_mode('off')` folds turn_on/
# turn_off into one reconcilable lever instead of a separate on/off path.

def hvac_mode_lever(climate_entity: str) -> str:
    return f"hvac_mode:{climate_entity}"



def fan_mode_lever(climate_entity: str) -> str:
    return f"fan_mode:{climate_entity}"
