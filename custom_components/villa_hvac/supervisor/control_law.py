"""Pure control law (C2 split): comfort-band + capacity fan, center composition,
duty cycle, demand coalescing, and the PV/energy pre-cool heuristic."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .arbiter import BLOCCO_BLOCK, BLOCCO_RELEASE




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



# --- #3 v2: comfort-band control + capacity-matched fan (pure) ---------------
# Two-axis control of a fancoil cooling unit:
#  - SETPOINT band: we impose a wide, settable hysteresis by slamming the
#    thermostat setpoint to center-A (RUN, valve forced open) / center+A (REST,
#    valve closed), flipping at center±B/2. Long uniform cycles, no bang-bang.
#  - FAN: sized to the thermal load (capacity-matched), so it's quiet where less
#    power is needed. The band guarantees comfort regardless of fan accuracy.


@dataclass(frozen=True)
class BandState:
    """Comfort-band hysteresis phase for one cooling unit."""

    phase: str = "released"   # "released" | "run" | "rest"



def band_step(
    phase: str,
    *,
    eligible: bool,
    temp: float | None,
    center: float | None,
    band: float,
    slam: float,
) -> tuple[str, float | None]:
    """Advance the comfort-band hysteresis. Returns (new_phase, setpoint).

    RUN once temp ≥ center + B/2 (drive setpoint to center-A → valve open);
    REST once temp ≤ center - B/2 (drive setpoint to center+A → valve closed);
    within the band, hold the current phase (the wide hysteresis). setpoint is
    None when released → no opinion, the house-mode policy owns the setpoint.
    """
    if not eligible or temp is None or center is None:
        return "released", None
    half = band / 2.0
    if temp >= center + half:
        phase = "run"
    elif temp <= center - half:
        phase = "rest"
    elif phase == "released":
        phase = "run" if temp >= center else "rest"
    # else: within the band -> keep the current phase (hysteresis hold)
    if phase == "run":
        return "run", round(center - slam, 2)
    return "rest", round(center + slam, 2)



def cooling_load(
    temp: float | None,
    outdoor: float | None,
    solar: float | None,
    *,
    a: float,
    b: float,
    c: float,
) -> float:
    """Heat-gain rate G (°C/h) = a·(T_out−T) + b·S + c. Missing inputs drop their
    term. Positive G = the room is gaining heat (needs cooling to hold)."""
    g = c
    if outdoor is not None and temp is not None:
        g += a * (outdoor - temp)
    if solar is not None:
        g += b * solar
    return g



def fan_level(u: float, fan_min_pct: int, *, step: int = 10) -> int:
    """Quantize a fan effort u∈[0,1] to a level (`step`% each), floored at the
    minimum-circulation %, capped at 100."""
    pct = max(0.0, min(1.0, u)) * 100.0
    quantized = round(pct / step) * step
    return int(min(100, max(fan_min_pct, quantized)))



def capacity_fan(
    load: float,
    *,
    pulldown: float,
    capacity: float,
    fan_min_pct: int,
    step: int = 10,
    last_level: int | None = None,
    hysteresis: int = 0,
) -> int:
    """Fan % to deliver (load + pulldown) of cooling given capacity k (°C/h at
    100%). Capacity ≤ 0 → full fan (can't size). Quantized to `step` levels.

    With `last_level`/`hysteresis`, holds the previous level until the raw demand
    moves past the level boundary by more than `step/2 + hysteresis` — so the fan
    doesn't hunt between adjacent levels as the load jitters."""
    if capacity <= 0:
        return 100
    raw = max(0.0, min(1.0, (load + pulldown) / capacity)) * 100.0
    level = round(raw / step) * step
    if last_level is not None and abs(raw - last_level) < (step / 2 + hysteresis):
        level = last_level
    return int(min(100, max(fan_min_pct, level)))



def effective_pulldown(
    temp: float | None,
    center: float | None,
    *,
    base: float,
    hours: float,
) -> float:
    """Required extraction rate (°C/h) during a RUN: the base pull-down rate plus
    the STORED-HEAT term — the room's excess over the band center spread over
    `hours`. The constant-rate law alone sized the fan for the instantaneous
    envelope gain only, which reads ~0 when the outdoor is cooler than the room:
    proven live 2026-07-03 08:25 (fan 0% in RUN, room 27.5 vs center 22) and
    2026-07-02 (fan 60% while padronale climbed to 30.2). A room 2 °C above
    center now demands base + 1 °C/h; far-above-center rooms saturate to 100%
    BY THE LAW, no clamp needed."""
    if temp is None or center is None or hours <= 0:
        return base
    return base + max(0.0, temp - center) / hours



def run_fan_pct(
    *,
    temp: float | None,
    outdoor: float | None,
    solar: float | None,
    center: float | None,
    band: float,
    a: float,
    b: float,
    c: float,
    k: float,
    pulldown: float,
    pulldown_hours: float,
    run_floor: int,
    fan_min_pct: int,
    at_peak: bool = False,
    step: int = 10,
    last_level: int | None = None,
    hysteresis: int = 0,
) -> int:
    """The ONE RUN-fan sizing law (2026-07-04): capacity-match the envelope gain
    PLUS the stored-heat extraction (`effective_pulldown`), floored at
    `run_floor` — a RUN with the fan at 0% is self-defeating (the KNX fancoil
    interlock holds the EV valve closed while the fan is off) — and clamped to
    100% when a room is losing ground above the band at the verified ~0-net
    outdoor peak, where the model's gain estimate cannot be trusted (the one
    place a guardrail backstops the law). Shared verbatim by the live band
    (trio + fold) and the planner room sim so plan and actuation never drift."""
    load = cooling_load(temp, outdoor, solar, a=a, b=b, c=c)
    pull = effective_pulldown(temp, center, base=pulldown, hours=pulldown_hours)
    pct = capacity_fan(
        load, pulldown=pull, capacity=k,
        fan_min_pct=max(fan_min_pct, run_floor), step=step,
        last_level=last_level, hysteresis=hysteresis,
    )
    if (
        at_peak and temp is not None and center is not None
        and temp >= center + band / 2.0
    ):
        return 100
    return pct



# --- Band-center composition (F4c Phase 1, pure) -----------------------------
# The band `center` for a cooling leader is composed from the base mode center
# (house_setpoint + mode_offset, incl. the #8 override) plus at most one feature
# per cycle, in a FIXED priority: PV bank / PV coast are mutually exclusive with
# the #9 pre-cool + F4b comfort-relax path. This is the single, named, testable
# home for that ladder (was inline in FanBandController) — so the composition is
# observable, unit-pinned, and the drop-in point the unified planner (Phase 6)
# replaces behind its switch. See COMPOSITION_ORDER in policies.py.
#
# INVARIANT: a comfort FLOOR bounds the LOWERING features (pv_bank, precool)
# symmetrically to how duty_comfort_max (the ceiling) bounds the RAISING features
# (pv_coast, comfort_relax). The base mode center itself is NOT ceiling-clamped —
# a Via/Notte setback center legitimately sits above the comfort ceiling.


@dataclass(frozen=True)
class CenterComposition:
    """How one leader's band center was composed this cycle (observability)."""

    center: float
    base: float           # house_setpoint + mode_offset (the mode's center)
    source: str           # base | pv_bank | pv_coast | precool | comfort_relax
    floored: bool = False  # the comfort floor clamped a lowering feature up



def compose_center(
    *,
    base: float,
    pv_mode: str | None,
    pv_floor: float | None,
    pv_coast_relax: float,
    comfort_enabled: bool,
    comfort_relax: float,
    precool: bool,
    precool_offset: float | None,
    duty_enabled: bool,
    comfort_ceiling: float | None,
    comfort_floor: float | None,
) -> CenterComposition:
    """Compose the band center from the base + the active feature, then apply the
    comfort floor. Behaviour-preserving replica of the old FanBandController ladder
    (PV bank/coast mutually exclusive with #9 pre-cool + F4b relax) + the floor.
    """
    center = base
    source = "base"
    if pv_mode == PRECOOL_BANK and pv_floor is not None:
        center = min(base, pv_floor)              # bank down toward the floor
        source = "pv_bank"
    elif pv_mode == PRECOOL_COAST:
        protected = comfort_enabled and comfort_relax == 0  # inside comfort window
        coast = 0.0 if protected else max(pv_coast_relax, comfort_relax)
        center = base + coast
        if comfort_ceiling is not None:            # raising feature capped at ceiling
            center = min(center, comfort_ceiling)
        source = "pv_coast" if coast else "base"
    else:
        if precool and duty_enabled and precool_offset is not None:
            center = base - precool_offset          # #9 pre-cool lowers
            source = "precool"
        if comfort_relax:                           # F4b relax (pre-capped by engine)
            center += comfort_relax
            if source == "base":
                source = "comfort_relax"
    floored = comfort_floor is not None and center < comfort_floor
    if floored:
        center = comfort_floor                      # LOWERING features bounded below
    return CenterComposition(center=center, base=base, source=source, floored=floored)



# --- F4b: comfort windows (pure) ---------------------------------------------


def in_window(minute_of_day: int, from_min: int, to_min: int) -> bool:
    """True if minute-of-day is within [from, to), handling windows that wrap past
    midnight (e.g. a bedroom 22:00→08:00). from==to means 'always'."""
    m = minute_of_day % 1440
    if from_min == to_min:
        return True
    if from_min < to_min:
        return from_min <= m < to_min
    return m >= from_min or m < to_min  # wraps midnight



# --- F3c: demand coalescing (pure) -------------------------------------------
# In MEDIUM load, sync all leader rooms to RUN together then REST together so the
# PdC does fewer, longer cycles. The house_phase is decided here from the room
# temps with wide enter/exit hysteresis + min compressor on/off floors. REST only
# when EVERY room is satisfied (a fast room must never force-rest a slow one), and
# a comfort breach forces RUN regardless. The band controller is told the phase
# via an explicit override; rest closes valves through the raised setpoint (NOT
# BLOCCO), so the fail-safe fully restores native KNX.


@dataclass(frozen=True)
class RegimeState:
    house_phase: str = "rest"            # "run" | "rest"
    run_started: datetime | None = None
    rest_started: datetime | None = None



def run_rest_durations(
    g: float, k: float, u: float, band: float
) -> tuple[timedelta | None, timedelta | None]:
    """Backstop estimate of run/rest length from the model: run = B/(k·u−G),
    rest = B/G. None when the net rate is non-positive. Clamped to ≤ 6 h.
    Diagnostic only — the temp crossing + min floors drive the actual coalescing."""
    cap = timedelta(hours=6)
    net = k * u - g
    run = min(cap, timedelta(hours=band / net)) if net > 0 else None
    rest = min(cap, timedelta(hours=band / g)) if g > 0 else None
    return run, rest



# --- PV/energy-aware daily pre-cool (F4c-lite, pure) -------------------------
# Bank coolth at the thermodynamically most effective hours (cool + low solar gain),
# using the solar forecast + battery as a buffer, so the hot/expensive evening needs
# minimal compressor. Comfort is a HARD bound both ways. The decision is symbolic
# (bank / coast / hold); the engine maps it onto the existing band-center + duty
# levers. Pure so the whole heuristic is unit-testable.

PRECOOL_BANK = "bank"     # cool now toward the floor (efficient + energy-smart hour)

PRECOOL_COAST = "coast"   # defer within comfort (actively inefficient hot hour)

PRECOOL_HOLD = "hold"     # no opinion (normal comfort band)



def cooling_effectiveness(
    t_room: float | None, t_out: float | None, solar: float | None,
    *, a: float, b: float, c: float, k: float,
) -> float:
    """Net cooling rate (°C/h) at full fan = k − gain, where
    gain = a(T_out−T) + b·S + c. ≤ 0 when gains overwhelm capacity (the ~34°C peak,
    where the fancoils were measured to net ~0). Higher in cool, low-sun hours."""
    return k - cooling_load(t_room, t_out, solar, a=a, b=b, c=c)



@dataclass(frozen=True)
class EnergyPrecoolDecision:
    """Result of the PV/energy pre-cool heuristic for the current cycle."""

    mode: str                 # PRECOOL_BANK / PRECOOL_COAST / PRECOOL_HOLD
    floor: float | None = None  # band-center floor to bank to (BANK only)
    solar_rich: bool = False
    eff_now: float = 0.0
    eff_peak: float = 0.0
    reason: str = ""



def energy_precool_decision(
    *, effectiveness: list[float], now_index: int = 0,
    pv_kwh_remaining: float | None, consumption_kwh_remaining: float | None,
    eff_fraction: float = 0.6, eff_min: float = 0.1,
    floor_rich: float = 22.0, floor_poor: float = 23.0,
) -> EnergyPrecoolDecision:
    """Decide bank / coast / hold for THIS cycle from an hourly effectiveness horizon.

    - `effectiveness[h]` = `cooling_effectiveness` for each forecast hour; the horizon
      is `effectiveness[now_index:]`.
    - Nothing effective ahead (`eff_peak ≤ eff_min`) → HOLD (leave it to the band).
    - `solar_rich` = forecast daily solar ≥ remaining consumption → bank deeper (free);
      solar-poor still banks in the efficient hours (grid-draw OK per the owner) but to
      a gentler floor to limit the draw.
    - Now among the efficient hours (`eff_now ≥ eff_fraction·eff_peak`) → BANK.
    - Now actively inefficient (`eff_now ≤ eff_min`, e.g. the hot peak) → COAST (defer).
    - Otherwise → HOLD.
    """
    horizon = effectiveness[now_index:]
    if not horizon:
        return EnergyPrecoolDecision(mode=PRECOOL_HOLD, reason="no-horizon")
    eff_now = horizon[0]
    eff_peak = max(horizon)
    if eff_peak <= eff_min:
        return EnergyPrecoolDecision(
            mode=PRECOOL_HOLD, eff_now=eff_now, eff_peak=eff_peak,
            reason="no-effective-hour",
        )
    # `consumption > 0` guards the end-of-day degenerate: both remaining-PV and
    # remaining-consumption decay to ~0, and 0 >= 0 would spuriously flip solar_rich
    # True — banking to the DEEPEST floor at night on pure grid. Require a real
    # remaining consumption to compare against.
    solar_rich = (
        pv_kwh_remaining is not None and consumption_kwh_remaining is not None
        and consumption_kwh_remaining > 0
        and pv_kwh_remaining >= consumption_kwh_remaining
    )
    if eff_now >= eff_fraction * eff_peak and eff_now > eff_min:
        return EnergyPrecoolDecision(
            mode=PRECOOL_BANK,
            floor=floor_rich if solar_rich else floor_poor,
            solar_rich=solar_rich, eff_now=eff_now, eff_peak=eff_peak,
            reason="efficient-hour" + ("-solar-rich" if solar_rich else "-grid-ok"),
        )
    if eff_now <= eff_min:
        return EnergyPrecoolDecision(
            mode=PRECOOL_COAST, solar_rich=solar_rich,
            eff_now=eff_now, eff_peak=eff_peak, reason="inefficient-hour-defer",
        )
    return EnergyPrecoolDecision(
        mode=PRECOOL_HOLD, solar_rich=solar_rich,
        eff_now=eff_now, eff_peak=eff_peak, reason="borderline",
    )



def coalesce_phase(
    rs: RegimeState, *,
    room_temps: dict[str, float], center: float, band: float, now: datetime,
    min_on: timedelta, min_off: timedelta,
    enter_frac: float, exit_frac: float, comfort_breach: bool,
) -> tuple[RegimeState, str]:
    """Advance the synchronized house RUN/REST. RUN when the hottest room rises to
    center + enter·B/2 (and min-off elapsed) or a comfort breach; REST only when
    EVERY room has fallen to center − exit·B/2 (and min-on elapsed)."""
    if not room_temps:
        return rs, rs.house_phase
    hottest = max(room_temps.values())
    half = band / 2.0
    if rs.house_phase == "run":
        elapsed = (now - rs.run_started) if rs.run_started else min_on
        if hottest <= center - exit_frac * half and elapsed >= min_on:
            return replace(rs, house_phase="rest", rest_started=now), "rest"
        return rs, "run"
    # currently resting
    elapsed = (now - rs.rest_started) if rs.rest_started else min_off
    if comfort_breach or (hottest >= center + enter_frac * half and elapsed >= min_off):
        return replace(rs, house_phase="run", run_started=now), "run"
    return rs, "rest"
