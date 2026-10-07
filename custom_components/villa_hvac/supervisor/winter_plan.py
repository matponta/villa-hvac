"""Winter setback advisor (W2) + PV surplus latch (W4) — pure, no HA imports.

W2 "quanto posso abbassare": for a return ETA, the DEEPEST uniform setback
(°C below each room's Casa target, never under `floor`) such that every room,
coasting down with the valve closed and then recovering on the radiant floor,
is back at its target by the ETA — plus WHEN the pre-heat must start.
W3 executes it (hold the depth, switch to Casa at `start`).

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


@dataclass(frozen=True)
class WinterRoom:
    zone: str
    name: str
    temp: float
    target: float           # the room's Casa target (house winter + room trim)
    params: WinterParams


@dataclass(frozen=True)
class RoomPlan:
    zone: str
    name: str
    target: float
    setback: float           # setpoint held while waiting
    start: datetime          # when this room must start recovering
    temp_at_start: float     # the lowest it actually gets (coast until start)
    saved_dh: float          # degree-hours below target (proxy for the saving)


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


def winter_setback_advice(
    rooms, *, now: datetime, eta: datetime | None, outdoor: float | None,
    floor: float, max_depth: float, min_depth: float = 0.0,
) -> WinterAdvice | None:
    """Hold the deepest allowed setback (≥ floor, ≤ max_depth below Casa) and
    start the house pre-heat at the earliest room start, so every room is back
    at its Casa target by the ETA. `drop` = how low the house actually gets."""
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
    depth = max(depth, min_depth, 0.0)
    plans = []
    for r in rooms:
        setback = r.target - depth
        start_h, t_pre = _solve_start(r, setback, hours, outdoor)
        plans.append(RoomPlan(
            zone=r.zone, name=r.name, target=r.target, setback=round(setback, 1),
            start=now + timedelta(hours=start_h), temp_at_start=round(t_pre, 1),
            saved_dh=_saved_dh(r, setback, start_h, outdoor),
        ))
    first = min(plans, key=lambda p: p.start)
    drop = max(0.0, max(p.target - p.temp_at_start for p in plans))
    return WinterAdvice(
        eta=eta, hours=round(hours, 1), outdoor=round(outdoor, 1),
        depth=round(depth, 1), drop=round(drop, 1), start=first.start,
        rooms=tuple(plans), limiting_room=first.name,
        saved_dh=round(sum(p.saved_dh for p in plans), 1),
    )


# --- W4: PV surplus latch -----------------------------------------------------------

SOC_START = 90.0
SOC_STAY = 85.0
SURPLUS_MIN_W = 300.0          # net outflow (export + charging − import − discharge)
DEFICIT_STOP_W = 1500.0        # net inflow (import + discharge) that ends it
DWELL_ON = timedelta(minutes=10)
DWELL_OFF = timedelta(minutes=10)
MIN_ON = timedelta(minutes=30)
MIN_OFF = timedelta(minutes=60)


@dataclass(frozen=True)
class PvHeatState:
    active: bool = False
    since: datetime | None = None          # when the current active/idle began
    cond_since: datetime | None = None     # start (or stop) condition held since
    reason: str = "idle"


def pv_surplus_step(
    state: PvHeatState, *, now: datetime, soc: float | None,
    grid_w: float | None, battery_w: float | None, sun_up: bool,
) -> PvHeatState:
    """Grid: + import / − export. Battery: − charging / + discharging.

    The judgement is on the NET Condominio flow `−(grid + battery)` — export and
    charging count, import and discharge subtract — so another apartment running
    the shared PdC, grid charging or one meter's spike can't fake a surplus.
    START: SoC ≥ SOC_START, sun up, net ≥ SURPLUS_MIN_W for DWELL_ON (after a
    MIN_OFF rest). STAY until: sun down / SoC < SOC_STAY (immediate), or a net
    deficit ≥ DEFICIT_STOP_W — our own heat pump eating the battery once the sun
    fades — or missing data, sustained DWELL_OFF (after MIN_ON)."""
    have = soc is not None and grid_w is not None and battery_w is not None
    if not state.active:
        if state.since is not None and now - state.since < MIN_OFF:
            return replace(state, cond_since=None, reason="rest")
        if not have:
            return replace(state, cond_since=None, reason="no data")
        net = -(grid_w + battery_w)
        if not (sun_up and soc >= SOC_START and net >= SURPLUS_MIN_W):
            return replace(state, cond_since=None, reason="no surplus")
        since = state.cond_since or now
        if now - since >= DWELL_ON:
            return PvHeatState(active=True, since=now, reason="surplus")
        return replace(state, cond_since=since, reason="surplus (dwell)")
    # active
    if not sun_up or (have and soc < SOC_STAY):
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
