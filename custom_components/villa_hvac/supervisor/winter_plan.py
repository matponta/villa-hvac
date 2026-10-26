"""Winter setback advisor (W2) + PV surplus latch (W4) — pure, no HA imports.

W2 "quanto posso abbassare": for a return ETA, the DEEPEST uniform setback
(°C below each room's Casa target, never under `floor`) such that every room,
coasting down with the valve closed and then recovering on the radiant floor,
is back at its target by the ETA — plus WHEN the pre-heat must start.
W3 executes it (hold the depth, switch to Casa at `start`).
v0.83.0: only the KEY rooms (switch.<zone>_return_key) whose plan is credible
drive the house start, never more than `max_lead` before the ETA — one
mis-modelled bathroom put the whole villa in Casa 57 h early (2026-10-22).

W4 PV surplus: a latch over the Condominio net flows (they include every
apartment, so another unit running the shared PdC removes the surplus by
itself): START on battery ≥ SoC_START and real net outflow (export or charging)
sustained; STAY while battery ≥ SoC_STAY, sun up and no sustained grid import.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .winter_model import WinterParams, coast_temp, recovery_minutes

BISECT_ITERS = 40
DEPTH_ITERS = 12       # v0.83.0: depth bisection (6 °C / 2^12 ≈ 0.002 °C)


@dataclass(frozen=True)
class WinterRoom:
    zone: str
    name: str
    temp: float
    target: float           # the room's Casa target (house winter + room trim)
    params: WinterParams
    key: bool = True        # v0.83.0: a room that must be warm at the ETA


@dataclass(frozen=True)
class RoomPlan:
    zone: str
    name: str
    target: float
    setback: float           # setpoint held while waiting
    start: datetime          # when this room must start recovering
    temp_at_start: float     # the lowest it actually gets (coast until start)
    saved_dh: float          # degree-hours below target (proxy for the saving)
    key: bool = True
    plannable: bool = True   # the model can recover it from the setback


@dataclass(frozen=True)
class WinterAdvice:
    eta: datetime
    hours: float
    outdoor: float
    depth: float             # setpoint depth applied below Casa (uniform, W3)
    drop: float              # how far the house ACTUALLY gets below Casa
    start: datetime          # house pre-heat start (earliest room)
    rooms: tuple[RoomPlan, ...] = ()
    limiting_room: str | None = None
    saved_dh: float = 0.0
    capped: bool = False     # the start was held back to ETA − max_lead
    # v0.85.0: what W3 writes — the predicted drop (never deeper than the house
    # actually gets), not below the native Via. `depth` jumps 3↔5 at the cap
    # boundary while the drop is continuous (live 23/10: setpoints 16↔18.1).
    setpoint_depth: float = 0.0


def outdoor_estimate(forecast, now: datetime, until: datetime, current: float | None):
    """Conservative outdoor for the absence: min(current, mean forecast in window)."""
    temps = [t for when, t in forecast or () if now <= when <= until]
    mean = sum(temps) / len(temps) if temps else None
    vals = [v for v in (current, mean) if v is not None]
    return min(vals) if vals else None


def _temp_at(room: WinterRoom, hours: float, outdoor: float, setback: float) -> float:
    return min(coast_temp(room.temp, outdoor, hours, room.params, floor=setback),
               room.temp)


def _solve_start(room: WinterRoom, setback: float, hours: float, outdoor: float):
    """Latest recovery start t (h from now) with t + recovery(T(t)) = hours.
    t + recovery(T(t)) grows with t (the room only gets colder), so bisect.
    Returns (t, T(t)); t = 0 when even starting now is not enough / possible."""
    def total(t: float) -> float | None:
        rec = recovery_minutes(_temp_at(room, t, outdoor, setback), room.target,
                               outdoor, room.params)
        return None if rec is None else t + rec / 60.0

    f0 = total(0.0)
    if f0 is None or f0 >= hours:
        return 0.0, room.temp
    lo, hi = 0.0, hours
    for _ in range(BISECT_ITERS):
        mid = (lo + hi) / 2
        f = total(mid)
        if f is not None and f <= hours:
            lo = mid
        else:
            hi = mid
    return lo, _temp_at(room, lo, outdoor, setback)


def _saved_dh(room: WinterRoom, setback: float, start_h: float, outdoor: float) -> float:
    total, h, dt = 0.0, 0.0, 0.25
    while h < start_h:
        total += max(0.0, room.target - _temp_at(room, h, outdoor, setback)) * dt
        h += dt
    return round(total, 1)


def _plan_rooms(rooms, depth: float, hours: float, outdoor: float, now: datetime):
    plans = []
    for r in rooms:
        setback = r.target - depth
        start_h, t_pre = _solve_start(r, setback, hours, outdoor)
        # Credible = the model can bring the room back from the setback at
        # all. Judged from the setback, not T(start): an unrecoverable fit
        # "solves" by never letting the room coast (start = now, the bug).
        plannable = recovery_minutes(
            min(r.temp, setback), r.target, outdoor, r.params) is not None
        plans.append(RoomPlan(
            zone=r.zone, name=r.name, target=r.target, setback=round(setback, 1),
            start=now + timedelta(hours=start_h), temp_at_start=round(t_pre, 1),
            saved_dh=_saved_dh(r, setback, start_h, outdoor),
            key=r.key, plannable=plannable,
        ))
    drivers = ([p for p in plans if p.key and p.plannable]
               or [p for p in plans if p.plannable])
    first = min(drivers, key=lambda p: p.start) if drivers else None
    return plans, first


def winter_setback_advice(
    rooms, *, now: datetime, eta: datetime | None, outdoor: float | None,
    floor: float, max_depth: float, min_depth: float = 0.0,
    max_lead: timedelta = timedelta(hours=12),
) -> WinterAdvice | None:
    """Hold the deepest allowed setback (≥ floor, ≤ max_depth below Casa) and
    start the house pre-heat at the earliest start of the KEY rooms, so they are
    back at their Casa target by the ETA; the other rooms follow the house
    (best effort). A room whose model cannot recover it from the setback at all
    (implausible fit) is not plannable and never drives the start; with no key
    room plannable the plannable others drive it.

    v0.83.0 the quadra: the pre-heat never starts more than `max_lead` before
    the ETA. When the deepest setback would need longer, the depth is made
    shallower (down to the native Via) until the key rooms recover within
    `max_lead`; only if even that is not enough is the start clamped
    (`capped`, the key rooms arrive a little short). `drop` = how low the house
    actually gets."""
    if eta is None or outdoor is None or eta <= now:
        return None
    rooms = [r for r in rooms if r.temp is not None and r.target is not None]
    if not rooms:
        return None
    hours = (eta - now).total_seconds() / 3600.0
    # One uniform depth, limited by the floor of the rooms that can still go
    # down (an Economy / trimmed room already at or under the floor must not
    # cap the whole house at 0 — review MAJOR), and never shallower than the
    # native Via (`min_depth`): arming the return must not warm an away house.
    room_room = [r.target - floor for r in rooms if r.target - floor > 0]
    depth = min(max_depth, min(room_room)) if room_room else 0.0
    min_depth = max(min_depth, 0.0)
    depth = max(depth, min_depth)
    earliest = max(now, eta - max_lead)
    plans, first = _plan_rooms(rooms, depth, hours, outdoor, now)
    if first is not None and first.start < earliest and depth > min_depth:
        # Shallower setback → the room coasts less → later start (monotone).
        lo, hi = min_depth, depth
        best = _plan_rooms(rooms, lo, hours, outdoor, now)
        if best[1] is None or best[1].start >= earliest:
            for _ in range(DEPTH_ITERS):
                mid = (lo + hi) / 2
                cand = _plan_rooms(rooms, mid, hours, outdoor, now)
                if cand[1] is None or cand[1].start >= earliest:
                    lo, best = mid, cand
                else:
                    hi = mid
            depth = lo
        else:
            depth = min_depth
        plans, first = best
    start = max(first.start, earliest) if first is not None else earliest
    drop = max(0.0, max(p.target - p.temp_at_start for p in plans))
    return WinterAdvice(
        eta=eta, hours=round(hours, 1), outdoor=round(outdoor, 1),
        depth=round(depth, 1), drop=round(drop, 1), start=start,
        rooms=tuple(plans), limiting_room=first.name if first else None,
        saved_dh=round(sum(p.saved_dh for p in plans), 1),
        capped=first is None or first.start < earliest,
        setpoint_depth=round(max(min_depth, min(depth, drop)), 1),
    )


# --- v0.85.0: hold the written depth (no setpoint flapping) -------------------

DEPTH_STEP = 0.5                     # °C — the written depth is quantised
DEPTH_DEADBAND = 0.5                 # °C — a change must exceed this...
DEPTH_SETTLE = timedelta(minutes=30) # ...continuously for this long


@dataclass(frozen=True)
class DepthHold:
    depth: float | None = None       # what W3 writes now
    since: datetime | None = None    # raw has been outside the deadband since


def hold_depth(h: DepthHold, raw: float, now: datetime, *,
               step: float = DEPTH_STEP, deadband: float = DEPTH_DEADBAND,
               settle: timedelta = DEPTH_SETTLE) -> DepthHold:
    """The first value is adopted at once (quantised to `step`). After that
    the written depth moves only when `raw` has stayed more than `deadband`
    away from it for `settle`, and then to the quantised latest value. Noise
    around a boundary (the limiting room alternating, the outdoor estimate
    wobbling, the cap boundary) never reaches the thermostats."""
    q = round(round(raw / step) * step, 1)
    if h.depth is None:
        return DepthHold(depth=q)
    if abs(raw - h.depth) <= deadband:
        return DepthHold(depth=h.depth)
    since = h.since or now
    if now - since >= settle:
        return DepthHold(depth=q)
    return DepthHold(depth=h.depth, since=since)


# --- W4: PV surplus latch -----------------------------------------------------------

SOC_START = 90.0
SOC_STAY = 85.0
SURPLUS_MIN_W = 300.0          # net outflow (export + charging − import − discharge)
STRONG_CHARGE_W = 1500.0       # v0.82.0 early start: net charge at least this…
FILL_MARGIN = 1.3              # …and the PV left today ≥ 1.3 × what fills the battery
DEFICIT_STOP_W = 1500.0        # net inflow (import + discharge) that ends it
DWELL_ON = timedelta(minutes=10)
DWELL_OFF = timedelta(minutes=10)
MIN_ON = timedelta(minutes=30)
MIN_OFF = timedelta(minutes=60)


def battery_fills_today(soc, pv_remaining_kwh, battery_kwh) -> bool:
    """True when the PV still to come today covers topping the battery up with
    margin — whatever we heat with now would otherwise be exported later."""
    if soc is None or pv_remaining_kwh is None or not battery_kwh:
        return False
    need = max(0.0, 100.0 - soc) / 100.0 * battery_kwh
    return pv_remaining_kwh >= FILL_MARGIN * need


@dataclass(frozen=True)
class PvHeatState:
    active: bool = False
    since: datetime | None = None          # when the current active/idle began
    cond_since: datetime | None = None     # start (or stop) condition held since
    reason: str = "idle"


def pv_surplus_step(
    state: PvHeatState, *, now: datetime, soc: float | None,
    grid_w: float | None, battery_w: float | None, sun_up: bool,
    pv_remaining_kwh: float | None = None, battery_kwh: float | None = None,
) -> PvHeatState:
    """Grid: + import / − export. Battery: − charging / + discharging.

    The judgement is on the NET Condominio flow `−(grid + battery)` — export and
    charging count, import and discharge subtract — so another apartment running
    the shared PdC, grid charging or one meter's spike can't fake a surplus.
    START (sun up, held DWELL_ON, after a MIN_OFF rest):
      * SoC ≥ SOC_START and net ≥ SURPLUS_MIN_W, or
      * v0.82.0 early start: net charge ≥ STRONG_CHARGE_W and the PV left today
        fills the battery anyway (`battery_fills_today`) — with a 41 kWh battery
        that starts the day empty, SoC 90 % arrives late afternoon or never.
    STAY until: sun down, or SoC < SOC_STAY while the battery would no longer
    fill (immediate); a net deficit ≥ DEFICIT_STOP_W — our own heat pump eating
    the battery — or missing data, sustained DWELL_OFF (after MIN_ON)."""
    have = soc is not None and grid_w is not None and battery_w is not None
    if not state.active:
        if state.since is not None and now - state.since < MIN_OFF:
            return replace(state, cond_since=None, reason="rest")
        if not have:
            return replace(state, cond_since=None, reason="no data")
        net = -(grid_w + battery_w)
        fills = battery_fills_today(soc, pv_remaining_kwh, battery_kwh)
        full = soc >= SOC_START and net >= SURPLUS_MIN_W
        early = fills and net >= STRONG_CHARGE_W
        if not (sun_up and (full or early)):
            return replace(state, cond_since=None, reason="no surplus")
        since = state.cond_since or now
        if now - since >= DWELL_ON:
            return PvHeatState(active=True, since=now, reason="surplus")
        return replace(state, cond_since=since, reason="surplus (dwell)")
    # active
    if not sun_up or (
        have and soc < SOC_STAY
        and not battery_fills_today(soc, pv_remaining_kwh, battery_kwh)
    ):
        return PvHeatState(active=False, since=now, reason="ended")
    bad = (not have) or (grid_w + battery_w) >= DEFICIT_STOP_W
    if not bad:
        return replace(state, cond_since=None, reason="surplus")
    if now - state.since < MIN_ON:
        return replace(state, reason="surplus (min on)")
    since = state.cond_since or now
    if now - since >= DWELL_OFF:
        return PvHeatState(active=False, since=now,
                           reason="ended (no data)" if not have else "ended (deficit)")
    return replace(state, cond_since=since,
                   reason="no data (dwell)" if not have else "deficit (dwell)")
