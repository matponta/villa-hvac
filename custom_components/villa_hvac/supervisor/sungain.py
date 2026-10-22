"""Winter solar gain (v0.78.0; v0.83.0 lost-sun put-back) — pure core. No HA imports.

Owner ask 2026-09-30: in winter, while the house is empty (Via / Vacanza) and
the sun is on a facade, open that facade's covers to let the free heat in; at
sunset put back down the covers WE opened (keep the heat in overnight).

EDGE semantics, never re-assert (lesson of v0.41's Via full-close, which fought
the owner through the 2 h override backoff):
  * each cover is opened at most ONCE per local day, and only while away;
  * a cover we opened that someone then moves clearly away from open (after a
    grace period, not while it is travelling) is dropped — the owner owns it
    now and it is NOT closed at sunset; same if its room gets shade-blocked;
  * a stop at home (Casa/Notte) does NOT forget what we opened (review
    v0.78.0: home 13:00, out again 14:00 with #2c still on Casa → the covers
    stayed open all night): sunset still puts back the covers we opened and
    nobody touched;
  * the switch/master off or the season leaving winter forgets everything
    without writing (hands off).

v0.83.0 (owner 2026-10-20: a 15-min sunny spell at 11:10 opened 8 covers that
then stayed up through the afternoon rain until sunset):
  * LOST SUN: the covers we opened go back to where they were (same put-back as
    sunset, any mode) once irradiance has stayed under the threshold for
    `dim_hold`, or at once while it rains;
  * no open while it rains;
  * a cover may be opened up to `max_opens` times per local day (owner: 3), so
    the sun coming back can still be used. Bounded: at most `max_opens` up/down
    cycles a day, each down only after `dim_hold` of no sun (or rain).
  * `done` now means only "hands off for today" (owner took it over, its room
    got shade-blocked, or it was already open when we first looked).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

OPEN_POSITION = 100
# A cover we opened reading below this is the owner's (moved it), not a cover
# that simply stopped a few % short of fully open.
TAKEOVER_MARGIN = 15


@dataclass(frozen=True)
class SunCover:
    """One shadeable cover as the winter-sun step sees it this cycle."""

    entity_id: str
    orientation: str | None
    blocked: bool = False
    position: int | None = None   # HA current_position (0 = down, 100 = open)
    moving: bool = False          # opening / closing right now


@dataclass(frozen=True)
class WinterSunState:
    """Bookkeeping carried across cycles (persisted by the HA wrapper)."""

    day: str | None = None                        # local ISO date of `done`/`opens`
    done: frozenset[str] = frozenset()            # hands off for the rest of today
    # covers WE opened -> (position before we opened, when we commanded it)
    opened: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    # how many times we opened each cover today (capped by `max_opens`)
    opens: dict[str, int] = field(default_factory=dict)
    # since when irradiance has been under the threshold (None = bright now)
    dim_since: datetime | None = None


def winter_sun_step(
    *,
    active: bool,
    away: bool,
    sun_elevation: float | None,
    sun_on: "set[str] | frozenset[str]",
    bright: bool,
    covers: "tuple[SunCover, ...] | list[SunCover]",
    now: datetime,
    local_day: str,
    state: WinterSunState,
    min_elevation: float,
    tolerance: float,
    grace: timedelta,
    raining: bool = False,
    dim_hold: timedelta = timedelta(minutes=30),
    max_opens: int = 3,
) -> tuple[WinterSunState, dict[str, int]]:
    """One cycle. Returns (new state, {cover entity_id: position to command}).

    `active` = master + switch + winter; `away` = Via or Vacanza (gates only
    the OPEN — put-backs and takeover run in any mode); `sun_on` = the facade
    orientations the sun is on now; `bright` = irradiance over threshold;
    `raining` = rain rate over its threshold.
    """
    new_day = state.day != local_day
    done = frozenset() if new_day else state.done
    opens = {} if new_day else dict(state.opens)
    dim_since = None if bright else (state.dim_since or now)
    if not active:
        # Feature off / master off / not winter: forget, write nothing.
        return WinterSunState(day=local_day, done=done, opens=opens), {}

    by_id = {c.entity_id: c for c in covers}
    opened = dict(state.opened)
    hands_off = set(done)
    # Owner takeover: a cover we opened that has settled clearly away from open,
    # or whose room is now shade-blocked, or that left the cover map -> it is
    # the owner's for the rest of the day (never re-opened, never put back).
    # A leftover from a previous day (e.g. sunset missed while HA was down) is
    # just forgotten: the new day must not start "hands off" because of it.
    for eid, (_prior, at) in list(opened.items()):
        c = by_id.get(eid)
        if c is None or c.blocked:
            del opened[eid]
            if not new_day:
                hands_off.add(eid)
            continue
        if (
            now - at >= grace and not c.moving and c.position is not None
            and c.position < OPEN_POSITION - TAKEOVER_MARGIN
        ):
            del opened[eid]
            if not new_day:
                hands_off.add(eid)

    def _out(opened_now):
        return WinterSunState(
            day=local_day, done=frozenset(hands_off), opened=opened_now,
            opens=opens, dim_since=dim_since,
        )

    commands: dict[str, int] = {}
    if sun_elevation is None:
        return _out(opened), commands

    sunset = sun_elevation <= 0
    lost_sun = raining or (dim_since is not None and now - dim_since >= dim_hold)
    if sunset or lost_sun:
        # Put back what we opened (to where it was), then forget it — whatever
        # the mode now (a cover nobody touched is still "ours").
        for eid, (prior, _at) in opened.items():
            commands[eid] = int(prior)
        # Nothing can open now: sunset, rain, or (long-)dim means not bright.
        return _out({}), commands

    if not away or sun_elevation <= min_elevation or not bright:
        return _out(opened), commands

    for c in covers:
        eid = c.entity_id
        if c.blocked or eid in hands_off or eid in opened or c.orientation not in sun_on:
            continue
        if c.position is None or c.moving:
            continue  # can't rule out a mid-travel owner action; try next cycle
        if c.position >= OPEN_POSITION - tolerance:
            if opens.get(eid, 0) == 0:
                hands_off.add(eid)  # already open before we ever touched it: owner's
            continue
        if opens.get(eid, 0) >= max_opens:
            continue
        commands[eid] = OPEN_POSITION
        opened[eid] = (int(c.position), now)
        opens[eid] = opens.get(eid, 0) + 1
    return _out(opened), commands
