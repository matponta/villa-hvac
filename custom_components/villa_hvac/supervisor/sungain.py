"""Winter solar gain (v0.78.0) — pure core. No HA imports.

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

    day: str | None = None                        # local ISO date of `done`
    done: frozenset[str] = frozenset()            # covers already handled today
    # covers WE opened -> (position before we opened, when we commanded it)
    opened: dict[str, tuple[int, datetime]] = field(default_factory=dict)


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
) -> tuple[WinterSunState, dict[str, int]]:
    """One cycle. Returns (new state, {cover entity_id: position to command}).

    `active` = master + switch + winter; `away` = Via or Vacanza (gates only
    the OPEN — sunset put-back and takeover run in any mode); `sun_on` = the
    facade orientations the sun is on now; `bright` = irradiance over threshold.
    """
    done = state.done if state.day == local_day else frozenset()
    if not active:
        # Feature off / master off / not winter: forget, write nothing.
        return WinterSunState(day=local_day, done=done), {}

    by_id = {c.entity_id: c for c in covers}
    opened = dict(state.opened)
    # Owner takeover: a cover we opened that has settled clearly away from open,
    # or whose room is now shade-blocked, or that left the cover map.
    for eid, (_prior, at) in list(opened.items()):
        c = by_id.get(eid)
        if c is None or c.blocked:
            del opened[eid]
            continue
        if (
            now - at >= grace and not c.moving and c.position is not None
            and c.position < OPEN_POSITION - TAKEOVER_MARGIN
        ):
            del opened[eid]

    commands: dict[str, int] = {}
    if sun_elevation is None:
        return WinterSunState(day=local_day, done=done, opened=opened), commands

    if sun_elevation <= 0:
        # Sunset: put back what we opened (to where it was), then forget it —
        # whatever the mode now (a cover nobody touched is still "ours").
        for eid, (prior, _at) in opened.items():
            commands[eid] = int(prior)
        return WinterSunState(day=local_day, done=done), commands

    if not away or sun_elevation <= min_elevation or not bright:
        return WinterSunState(day=local_day, done=done, opened=opened), commands

    new_done = set(done)
    for c in covers:
        if c.blocked or c.entity_id in new_done or c.orientation not in sun_on:
            continue
        if c.position is None or c.moving:
            continue  # can't rule out a mid-travel owner action; try next cycle
        new_done.add(c.entity_id)
        if c.position < OPEN_POSITION - tolerance:
            commands[c.entity_id] = OPEN_POSITION
            opened[c.entity_id] = (int(c.position), now)
    return (
        WinterSunState(day=local_day, done=frozenset(new_done), opened=opened),
        commands,
    )
