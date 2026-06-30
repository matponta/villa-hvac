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
    precool: bool = False,
) -> tuple[DutyState, str]:
    """Advance the duty cycle. Returns (new_state, desired BLOCCO value).

    - Duty-adaptive: at peak (hot outside) DON'T coalesce — let the PdC run and
      lean on load reduction (#6/#7); the gain-limited rooms can't afford a rest.
    - Pre-cool (forecast feed-forward): a hot peak is coming soon -> don't rest,
      keep banking coolth.
    - In a cooloff: keep blocking until it elapses (or a comfort breach aborts it).
    - Not cooling, or a room too hot: never block; reset the stint.
    - Cooling + comfortable: accumulate the stint; once it exceeds `max_stint`,
      begin a `cooloff` (block). Comfort always wins over the timer.
    """
    if at_peak or precool:
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


# --- #9 forecast run-window planner (pure) -----------------------------------


@dataclass(frozen=True)
class RunPlan:
    """Forecast-derived plan for the cooling run window (re-planned each refresh)."""

    precool: bool = False               # bank coolth now ahead of a coming peak
    forecast_peak: float | None = None  # max forecast temp over the lookahead
    peak_eta: timedelta | None = None   # time until that peak


def plan_run(
    forecast: list[tuple[datetime, float]],
    now: datetime,
    current_outdoor: float | None,
    *,
    peak_threshold: float,
    lookahead: timedelta,
    margin: float,
) -> RunPlan:
    """Build the run plan over the lookahead horizon (e.g. 12 h).

    Find the forecast peak in [now, now+lookahead]. Pre-cool when a HOT peak
    (>= peak_threshold) is still ahead AND it is currently at least `margin`
    cooler than that peak — i.e. bank coolth in the cool hours, and taper as the
    peak nears (once we're within `margin` of it, stop; peak-skip takes over).
    The long lookahead lets a high-mass house start early; the margin keeps it
    from pre-cooling all day.
    """
    window = [
        (when, t)
        for (when, t) in forecast
        if now <= when <= now + lookahead and t is not None
    ]
    if not window:
        return RunPlan()
    peak_when, peak_temp = max(window, key=lambda wt: wt[1])
    eta = peak_when - now
    if peak_temp < peak_threshold or current_outdoor is None:
        return RunPlan(precool=False, forecast_peak=peak_temp, peak_eta=eta)
    precool = peak_when > now and (peak_temp - current_outdoor) >= margin
    return RunPlan(precool=precool, forecast_peak=peak_temp, peak_eta=eta)


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
    """A shadeable cover, resolved from the registries (#6).

    `target_position` is the per-room shade target (HA cover position: 0 = fully
    closed/down, 100 = open) the blind is driven to when shading triggers; None
    means "use the house default". `blocked` is the per-room manual override —
    when True the cover is skipped entirely (not closed, not reopened).
    """

    entity_id: str
    orientation: str            # north / east / south / west (device label)
    zone: str | None = None     # area_id
    floor: str | None = None    # area.floor_id
    target_position: int | None = None  # per-room shade target (HA position)
    blocked: bool = False       # per-room manual override -> skip shading


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
    shading_default_position: int | None = None  # #6 fallback shade position
    duty_enabled: bool = False          # #9 duty-cycle switch
    duty_max_stint: timedelta | None = None
    duty_cooloff: timedelta | None = None
    duty_comfort_max: float | None = None  # abort cooloff if a zone exceeds this
    duty_peak_outdoor: float | None = None  # at/above this outdoor temp -> no duty
    precool: bool = False               # #9 forecast: hot peak imminent
    precool_offset: float | None = None  # °C below target while pre-cooling
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


# --- #11 plan view (pure) ----------------------------------------------------
# Project the organism's next-12h INTENT into a single structured view a
# dashboard can render: the forecast curve + peak, the pre-cool / peak-skip /
# duty run-rest regime, and each zone's planned setpoint. Pure so it is fully
# unit-testable and so it can be computed every cycle (read-only) even while the
# supervisor is deploy-dark — letting us watch the plan before lighting up the
# actuation. `desired` is the merged output of the PURE policy stack only (no
# stateful controllers), so computing it never advances duty/pacing timers.

# Season string (mirror of const.SEASON_SUMMER), kept local so this module
# stays import-pure (const.py imports homeassistant).
_SEASON_SUMMER = "summer"


@dataclass(frozen=True)
class ZonePlan:
    """Per-zone slice of the plan: where it is and where the plan wants it."""

    zone_id: str
    name: str
    temp: float | None
    target: float | None        # planned setpoint (from the policy stack), if any
    demand: bool | None         # EV FAN valve open = actually cooling now
    enabled: bool
    paused: bool


@dataclass(frozen=True)
class PlanView:
    """The next-12h plan, projected from one HouseState + RunPlan + DutyState."""

    summary: str                # one-word regime (the sensor state)
    season: str | None
    house_mode: str | None
    cooling: bool               # consenso_freddo on right now
    free_cool: bool             # #5 coasting (cool enough outside)
    precool: bool               # #9 banking coolth ahead of a peak
    at_peak: bool               # hot now -> peak-skip (let the PdC run)
    forecast_peak: float | None
    peak_eta: timedelta | None
    house_setpoint: float | None
    effective_setpoint: float | None   # base + mode offset
    precool_setpoint: float | None     # effective - precool offset (while pre-cooling)
    in_cooloff: bool            # #9 duty currently resting (BLOCCO block)
    cooloff_until: datetime | None
    stint_start: datetime | None
    stint_elapsed: timedelta | None
    stint_cap: timedelta | None
    blocco: str | None          # current central-block state
    blocco_desired: str | None  # what the duty regime wants the block to be
    duty_enabled: bool
    zones: tuple[ZonePlan, ...]
    covers_closing: tuple[str, ...]   # covers the shading policy is closing now
    forecast: tuple[tuple[datetime, float], ...]  # windowed curve for the timeline


def _is_free_cooling(state: HouseState) -> bool:
    return (
        state.free_cool_enabled
        and state.season == _SEASON_SUMMER
        and state.outdoor_temp is not None
        and state.free_cool_threshold is not None
        and state.outdoor_temp < state.free_cool_threshold
    )


def _plan_summary(
    *,
    season: str | None,
    cooling: bool,
    heating: bool,
    free_cool: bool,
    precool: bool,
    at_peak: bool,
    in_cooloff: bool,
) -> str:
    """Collapse the regime to a single word (the sensor's state).

    Summer precedence: free_cool > pre_cool > peak_run > duty_rest > cooling >
    idle. (free-cool, pre-cool and peak are effectively mutually exclusive, but
    the order makes the dominant intent unambiguous.) Winter only reflects the
    heating call for now — the anticipatory pre-heat (#7) is not built yet.
    """
    if season != _SEASON_SUMMER:
        return "heating" if heating else "idle"
    if free_cool:
        return "free_cool"
    if precool:
        return "pre_cool"
    if at_peak:
        return "peak_run"
    if in_cooloff:
        return "duty_rest"
    if cooling:
        return "cooling"
    return "idle"


def build_plan(
    state: HouseState,
    run_plan: RunPlan,
    desired: dict[str, str | float | None],
    duty: DutyState,
    forecast: list[tuple[datetime, float]],
    lookahead: timedelta,
) -> PlanView:
    """Project one cycle's state into the dashboard PlanView (pure).

    `desired` must be the PURE policy stack's merged output (presets/setpoints/
    covers) — NOT including the stateful Duty/Fan controllers — so building the
    plan has no side effects. Duty run/rest windows come from `duty`, the live
    DutyState (only meaningful while #9 is actually running).
    """
    free_cool = _is_free_cooling(state)
    cooling = state.consenso_freddo == "on"
    heating = state.consenso_caldo == "on"
    at_peak = (
        state.outdoor_temp is not None
        and state.duty_peak_outdoor is not None
        and state.outdoor_temp >= state.duty_peak_outdoor
    )

    effective = None
    if state.house_setpoint is not None and state.mode_offset is not None:
        effective = round(state.house_setpoint + state.mode_offset, 1)
    precool_setpoint = None
    if run_plan.precool and effective is not None and state.precool_offset is not None:
        precool_setpoint = round(effective - state.precool_offset, 1)

    in_cooloff = duty.cooloff_until is not None
    stint_elapsed = (
        state.now - duty.stint_start if duty.stint_start is not None else None
    )
    if not state.duty_enabled:
        blocco_desired = None
    elif at_peak or run_plan.precool:
        blocco_desired = BLOCCO_RELEASE
    elif in_cooloff:
        blocco_desired = BLOCCO_BLOCK
    else:
        blocco_desired = BLOCCO_RELEASE

    zones = tuple(
        ZonePlan(
            zone_id=z.zone_id,
            name=z.name,
            temp=z.temp,
            target=(
                desired.get(temperature_lever(z.climate)) if z.climate else None
            ),
            demand=z.demand,
            enabled=z.enabled,
            paused=z.paused,
        )
        for z in state.zones.values()
    )
    covers_closing = tuple(
        cover.entity_id
        for cover in state.covers
        if cover_lever(cover.entity_id) in desired
    )
    window = tuple(
        (when, t)
        for (when, t) in forecast
        if state.now <= when <= state.now + lookahead and t is not None
    )

    return PlanView(
        summary=_plan_summary(
            season=state.season,
            cooling=cooling,
            heating=heating,
            free_cool=free_cool,
            precool=run_plan.precool,
            at_peak=at_peak,
            in_cooloff=in_cooloff,
        ),
        season=state.season,
        house_mode=state.house_mode,
        cooling=cooling,
        free_cool=free_cool,
        precool=run_plan.precool,
        at_peak=at_peak,
        forecast_peak=run_plan.forecast_peak,
        peak_eta=run_plan.peak_eta,
        house_setpoint=state.house_setpoint,
        effective_setpoint=effective,
        precool_setpoint=precool_setpoint,
        in_cooloff=in_cooloff,
        cooloff_until=duty.cooloff_until,
        stint_start=duty.stint_start,
        stint_elapsed=stint_elapsed,
        stint_cap=state.duty_max_stint,
        blocco=state.blocco,
        blocco_desired=blocco_desired,
        duty_enabled=state.duty_enabled,
        zones=zones,
        covers_closing=covers_closing,
        forecast=window,
    )
