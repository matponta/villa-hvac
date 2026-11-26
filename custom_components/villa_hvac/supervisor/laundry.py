"""Laundry drying through the lavanderia vasistas (v0.97.0, STORY_LAUNDRY.md) —
pure core. No HA imports.

Incident 25–26/11/2026: laundry hung in the lavanderia. The legacy HA automation
("Vasistas Lavanderia a/c 3") opened the vasistas on a humidity RISE and armed a
40-min timer; its close trigger fires once, when the rise settles — which
happened while the timer still ran, so the close was dropped and the vasistas
stayed open 23:47 → 07:47 (the 8 h safety). The #4 window pause held the room at
building_protection (7 °C) all night, 6.5 °C outside: cold room, wet laundry.

The physics the law follows:
- Drying needs WARM air + exchange. Outside air removes water only when its
  ABSOLUTE humidity is lower than the room's — winter fog at 7 °C / 96 % is
  7.6 g/m³ against a 19 °C / 50 % room at 8.3 g/m³: almost nothing to gain
  until the laundry pushes the room up (55 % at 20 °C ≈ 9.5 g/m³).
- So: short PURGES (open a few minutes, close), heating kept on, repeated while
  the laundry keeps raising the room — never one long opening.

The law, per cycle:
- PURGE starts when the vasistas is closed and has rested `min_closed`, the room
  RH ≥ `rh_start`, outside is ≥ `ah_margin` g/m³ drier, it isn't raining and the
  room is at least `room_min_temp` warm.
- A purge ends at `rh_stop` (after `min_open`), when outside stops being drier
  (`ah_margin / 2` left), on rain, on lost sensors, or at `max_open` — whichever
  comes first.
- Timings follow the outdoor temperature: below `cold_outdoor` the openings are
  short and the rests long (heat budget), above it a purge may last longer.
- An opening that is NOT ours (by hand, an app, a legacy automation) is left
  alone up to `manual_max_open` — short when cold, long when mild — then closed.
- Feature off: a purge of ours is closed (we opened it, we close it), then hands
  off. A manual opening is never touched while off.
- The vasistas moving, or its state unknown: hold, decide nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .ventilation import absolute_humidity

CMD_OPEN = "open"
CMD_CLOSE = "close"

# Reasons (Italian — they surface verbatim as the sensor state).
REASON_OFF = "spento"
REASON_IDLE = "chiuso"              # closed, nothing to dry
REASON_REST = "pausa"               # closed, resting between purges
REASON_WAIT_OUTSIDE = "fuori_umido"  # humid room, but outside is not drier
REASON_WAIT_RAIN = "pioggia"
REASON_WAIT_COLD_ROOM = "stanza_fredda"
REASON_PURGE = "ricambio"           # our purge is running
REASON_MANUAL = "aperto_a_mano"
REASON_BLIND = "sensori_ko"
REASON_UNKNOWN = "vasistas_ignoto"  # cover unavailable / moving
# Why a close was commanded (view only).
CLOSE_DRY = "asciutto"
CLOSE_MAX = "tempo_massimo"
CLOSE_OUTSIDE = "fuori_non_piu_asciutto"
CLOSE_RAIN = "pioggia"
CLOSE_BLIND = "sensori_ko"
CLOSE_MANUAL_MAX = "aperto_a_mano_troppo"
CLOSE_DISABLED = "disattivato"


@dataclass(frozen=True)
class LaundryParams:
    rh_start: float = 55.0
    rh_stop: float = 50.0
    ah_margin: float = 1.0                  # g/m³ outside must be drier by
    room_min_temp: float = 17.0             # never cool a room already this cold
    cold_outdoor: float = 15.0              # °C: below = cold timings
    min_open: timedelta = timedelta(minutes=5)
    max_open_cold: timedelta = timedelta(minutes=15)
    max_open_mild: timedelta = timedelta(minutes=60)
    min_closed_cold: timedelta = timedelta(minutes=20)
    min_closed_mild: timedelta = timedelta(minutes=10)
    manual_max_open_cold: timedelta = timedelta(minutes=60)
    manual_max_open_mild: timedelta = timedelta(hours=8)
    grace: timedelta = timedelta(seconds=90)  # cover still reporting the old state


@dataclass(frozen=True)
class LaundryState:
    """Bookkeeping carried across cycles (persisted by the HA wrapper)."""

    ours: bool = False                    # the current opening is our purge
    open_since: datetime | None = None    # when the current opening began
    closed_since: datetime | None = None  # when the vasistas last closed
    commanded_at: datetime | None = None  # our last open/close command
    commanded: str | None = None          # CMD_OPEN / CMD_CLOSE (for the grace)
    purges_day: str | None = None         # local ISO date of `purges`
    purges: int = 0                       # purges started that day (view only)


@dataclass(frozen=True)
class LaundryReading:
    """One cycle's inputs. `is_open` None = cover unavailable/unknown."""

    is_open: bool | None
    moving: bool
    room_temp: float | None
    room_rh: float | None
    outdoor_temp: float | None
    outdoor_rh: float | None
    raining: bool = False


@dataclass(frozen=True)
class LaundryDecision:
    state: LaundryState
    command: str | None
    reason: str
    close_reason: str | None
    ah_room: float | None
    ah_outdoor: float | None
    cold: bool
    max_open: timedelta | None       # the cap of the CURRENT opening (None = closed)
    next_purge_at: datetime | None   # earliest next purge while resting


def _cold(outdoor_temp: float | None, p: LaundryParams) -> bool:
    # Unknown outdoor temperature = assume cold (the short, safe caps).
    return outdoor_temp is None or outdoor_temp < p.cold_outdoor


def laundry_step(
    *,
    enabled: bool,
    reading: LaundryReading,
    state: LaundryState,
    now: datetime,
    local_day: str,
    params: LaundryParams = LaundryParams(),
) -> LaundryDecision:
    """One cycle. Returns the new state and at most one command."""
    p = params
    purges = state.purges if state.purges_day == local_day else 0
    st = replace(state, purges_day=local_day, purges=purges)
    ah_room = absolute_humidity(reading.room_temp, reading.room_rh)
    ah_out = absolute_humidity(reading.outdoor_temp, reading.outdoor_rh)
    cold = _cold(reading.outdoor_temp, p)
    max_open = p.max_open_cold if cold else p.max_open_mild
    manual_max = p.manual_max_open_cold if cold else p.manual_max_open_mild
    min_closed = p.min_closed_cold if cold else p.min_closed_mild

    def out(new: LaundryState, command: str | None, reason: str,
            close_reason: str | None = None, cap: timedelta | None = None,
            next_at: datetime | None = None) -> LaundryDecision:
        return LaundryDecision(
            new, command, reason, close_reason, ah_room, ah_out, cold, cap, next_at,
        )

    in_grace = (
        st.commanded_at is not None and now - st.commanded_at < p.grace
    )

    # Unknown or travelling: hold everything.
    if reading.is_open is None or reading.moving:
        return out(st, None, REASON_UNKNOWN)

    # A command still in flight (the cover hasn't reported the move yet): wait.
    # Past the grace a close that never landed is simply re-commanded.
    if in_grace and (
        (st.commanded == CMD_OPEN and not reading.is_open)
        or (st.commanded == CMD_CLOSE and reading.is_open)
    ):
        reason = REASON_PURGE if st.ours else REASON_MANUAL
        if not enabled and st.commanded == CMD_CLOSE:
            reason = REASON_OFF
        return out(st, None, reason, cap=max_open if st.ours else manual_max)

    # Track the opening/closing edges.
    if reading.is_open:
        if st.open_since is None:
            # A new opening we didn't command (ours is set when we command).
            st = replace(st, ours=False, open_since=now, closed_since=None)
    else:
        if st.open_since is not None or st.ours:
            st = replace(st, ours=False, open_since=None, closed_since=now)
        elif st.closed_since is None:
            # First look (boot): treat as rested so a humid room is served.
            st = replace(st, closed_since=now - min_closed)

    if reading.is_open:
        opened_for = now - st.open_since
        if not enabled:
            if st.ours:
                return _close(st, now, out, CLOSE_DISABLED, REASON_OFF)
            return out(st, None, REASON_OFF)
        if not st.ours:
            if opened_for >= manual_max:
                return _close(st, now, out, CLOSE_MANUAL_MAX, REASON_MANUAL)
            return out(st, None, REASON_MANUAL, cap=manual_max)
        # Our purge.
        if opened_for >= max_open:
            return _close(st, now, out, CLOSE_MAX, REASON_PURGE)
        if reading.raining:
            return _close(st, now, out, CLOSE_RAIN, REASON_PURGE)
        if opened_for < p.min_open:
            return out(st, None, REASON_PURGE, cap=max_open)
        if ah_room is None or ah_out is None:
            return _close(st, now, out, CLOSE_BLIND, REASON_PURGE)
        if reading.room_rh <= p.rh_stop:
            return _close(st, now, out, CLOSE_DRY, REASON_PURGE)
        if ah_out > ah_room - p.ah_margin / 2:
            return _close(st, now, out, CLOSE_OUTSIDE, REASON_PURGE)
        return out(st, None, REASON_PURGE, cap=max_open)

    # Closed.
    if not enabled:
        return out(st, None, REASON_OFF)
    if ah_room is None or ah_out is None:
        return out(st, None, REASON_BLIND)
    if reading.room_rh < p.rh_start:
        return out(st, None, REASON_IDLE)
    if reading.raining:
        return out(st, None, REASON_WAIT_RAIN)
    if ah_out > ah_room - p.ah_margin:
        return out(st, None, REASON_WAIT_OUTSIDE)
    if reading.room_temp < p.room_min_temp:
        return out(st, None, REASON_WAIT_COLD_ROOM)
    rest_until = (st.closed_since or now) + min_closed
    if now < rest_until:
        return out(st, None, REASON_REST, next_at=rest_until)
    st = replace(
        st, ours=True, open_since=now, closed_since=None,
        commanded=CMD_OPEN, commanded_at=now, purges=st.purges + 1,
    )
    return out(st, CMD_OPEN, REASON_PURGE, cap=max_open)


def _close(st: LaundryState, now: datetime, out, why: str, reason: str) -> LaundryDecision:
    # `ours` stays as it was until the cover reports closed (the edge tracker
    # then clears it): a close we commanded that never lands is retried.
    st = replace(st, commanded=CMD_CLOSE, commanded_at=now)
    return out(st, CMD_CLOSE, reason, close_reason=why)


def failed_command(before: LaundryState, after: LaundryState, command: str) -> LaundryState:
    """The write of `command` failed: undo what commanding it recorded, so the
    next cycle decides again from the real cover state."""
    if command == CMD_OPEN:
        return replace(
            after, ours=False, open_since=None, closed_since=before.closed_since,
            commanded=None, commanded_at=None, purges=max(0, after.purges - 1),
        )
    return replace(after, commanded=None, commanded_at=None)
