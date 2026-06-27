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
lives in engine.py; nothing here imports homeassistant.

Deploy-dark (decided 2026-06-27): the master `switch.supervisor` gates the
ENTIRE engine — including the migrated #2/#4/#10 — not just the new optimization
layer. On deploy nothing actuates until the switch is flipped, then the whole
organism lights up at once. Every controller path therefore routes through the
engine and is a no-op while the master is off.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
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


# --- #9 central duty-cycle (pure) --------------------------------------------
# Cap the villa's continuous cooling stint; when it's exceeded, force a cooloff
# via the Consenso BLOCCO, then release. This also SYNCHRONIZES the rooms — the
# whole villa runs together during the stint and rests together during cooloff.


@dataclass(frozen=True)
class DutyState:
    """Duty-cycle bookkeeping across cycles."""

    stint_start: datetime | None = None   # when the current cooling stint began
    cooloff_until: datetime | None = None  # block until this time


def duty_decision(
    cooling_active: bool,
    comfort_breach: bool,
    now: datetime,
    state: DutyState,
    max_stint: timedelta,
    cooloff: timedelta,
    at_peak: bool = False,
) -> tuple[DutyState, str]:
    """Advance the duty cycle. Returns (new_state, desired BLOCCO value).

    - Duty-adaptive: at peak (hot outside) DON'T coalesce — let the PdC run and
      lean on load reduction (#6/#7); the gain-limited rooms can't afford a rest.
    - In a cooloff: keep blocking until it elapses (or a comfort breach aborts it).
    - Not cooling, or a room too hot: never block; reset the stint.
    - Cooling + comfortable: accumulate the stint; once it exceeds `max_stint`,
      begin a `cooloff` (block). Comfort always wins over the timer.
    """
    if at_peak:
        return DutyState(), BLOCCO_RELEASE
    if state.cooloff_until is not None:
        if comfort_breach or now >= state.cooloff_until:
            return DutyState(), BLOCCO_RELEASE
        return state, BLOCCO_BLOCK
    if not cooling_active or comfort_breach:
        return DutyState(), BLOCCO_RELEASE
    start = state.stint_start or now
    if now - start >= max_stint:
        return DutyState(cooloff_until=now + cooloff), BLOCCO_BLOCK
    return DutyState(stint_start=start), BLOCCO_RELEASE


# --- #3 fan pacing (pure) ----------------------------------------------------
# Within a cooling run, hold the room's fan at a paced speed instead of letting
# the valve bang-bang: pull down hard while far from target, then drop to a
# maintenance speed near it. Two-phase hysteresis (approach / maintain).


def pacing_decision(
    phase: str,
    error: float,
    *,
    approach_band: float,
    maintain_band: float,
    approach_pct: int,
    maintain_pct: int,
) -> tuple[str, int]:
    """Advance the two-phase fan pacing. `error` = temp - target (°C).

    APPROACH (pull down) at approach_pct until error <= maintain_band; MAINTAIN
    at maintain_pct until error >= approach_band again. The band gap gives
    hysteresis so the fan doesn't flap at the setpoint. Returns (phase, fan %).
    """
    if phase == "maintain":
        if error >= approach_band:
            return "approach", approach_pct
        return "maintain", maintain_pct
    # approach (default)
    if error <= maintain_band:
        return "maintain", maintain_pct
    return "approach", approach_pct


# --- House-state model (pure data) -------------------------------------------
# The Supervisor builds one snapshot per cycle; policies read it and return
# desired lever settings. Keep this a plain data carrier — building it from
# Home Assistant lives in engine.py so this module stays import-pure.


@dataclass(frozen=True)
class ZoneSnapshot:
    """Per-zone slice of the house state."""

    zone_id: str
    name: str
    climate: str | None
    emitter: str | None
    temp: float | None = None      # fused current temperature (#1)
    demand: bool | None = None     # EV FAN valve open = actually cooling
    enabled: bool = True           # #10 zone enable switch
    paused: bool = False           # #4 window pause
    bedroom: bool = False          # camere silenziose zone (#2b)
    fancoil: str | None = None     # fan entity (for #3 pacing)
    manuale: str | None = None     # manuale switch entity (for #3 pacing)


@dataclass(frozen=True)
class CoverInfo:
    """A shadeable cover, resolved from the registries (#6)."""

    entity_id: str
    orientation: str            # north / east / south / west (device label)
    zone: str | None = None     # area_id
    floor: str | None = None    # area.floor_id


@dataclass(frozen=True)
class HouseState:
    """Unified per-cycle snapshot the policy stack reasons over."""

    now: datetime
    zones: dict[str, ZoneSnapshot] = field(default_factory=dict)
    covers: tuple[CoverInfo, ...] = ()
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    shading_enabled: bool = False
    shading_solar_threshold: float | None = None
    duty_enabled: bool = False          # #9 duty-cycle switch
    duty_max_stint: timedelta | None = None
    duty_cooloff: timedelta | None = None
    duty_comfort_max: float | None = None  # abort cooloff if a zone exceeds this
    duty_peak_outdoor: float | None = None  # at/above this outdoor temp -> no duty
    night_active: bool = False          # #2b camere silenziose in effect
    fan_pacing_enabled: bool = False    # #3 fan pacing switch
    season: str | None = None          # summer / winter
    house_mode: str | None = None      # Casa / Via / Notte / Vacanza
    auto_setback: bool = True          # #2 global Auto setback switch
    house_setpoint: float | None = None  # dashboard slider base setpoint
    mode_offset: float | None = None   # season-aware offset for house_mode
    free_cool_enabled: bool = False    # #5 outdoor free-cooling shutoff
    free_cool_threshold: float | None = None  # outdoor below this -> suppress
    outdoor_temp: float | None = None  # Ecowitt gw3000a
    solar: float | None = None         # Ecowitt solar radiation W/m²
    consenso_freddo: str | None = None
    consenso_caldo: str | None = None
    blocco: str | None = None          # central BLOCCO switch state


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
