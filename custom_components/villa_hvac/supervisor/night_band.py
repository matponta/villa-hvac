"""v0.96.0 night band (STORY_NIGHT_SCHEDULE.md) — pure core, no HA imports.

"Notte" is no longer a house mode. In Casa, a clock window (default 21:30 ->
07:00) trims every room's setpoint by the season's night offset. With an HA
alarm set, each room leaves the band `lead` before the wake time so it is at
comfort when the owner gets up; the lead comes from the room's winter model
(recovery from target-|offset| to target), clamped to [0, max_lead].

All arithmetic is in UTC: the window edges are built on the LOCAL wall clock
(so 21:30 is 21:30 across a DST change) and converted before comparing —
Python compares two datetimes sharing one tzinfo on their naive wall times,
which is wrong across the change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo

# A wake time this long after the band start is not "tomorrow morning" (an
# alarm left at 21:00 would otherwise stretch the band to the next evening).
NIGHT_BAND_MAX_WAKE_SPAN = timedelta(hours=14)
# Winter model confidence below this -> the lead falls back to max_lead.
NIGHT_LEAD_MIN_CONFIDENCE = 0.5

BAND_OFF = "spenta"          # switch off
BAND_IDLE = "inattiva"       # outside the window / not Casa / Auto setback off
BAND_ACTIVE = "attiva"       # every trimmed room in the band
BAND_PREHEAT = "preriscaldo"  # some room has already left the band for the wake


def _at(day: date, t: time, tz: tzinfo) -> datetime:
    """Local wall-clock `t` on `day`, as UTC."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(timezone.utc)


def _next_at(after: datetime, t: time, tz: tzinfo) -> datetime:
    """First local `t` strictly after the UTC instant `after`, as UTC."""
    local_day = after.astimezone(tz).date()
    for offset in range(0, 3):
        cand = _at(local_day + timedelta(days=offset), t, tz)
        if cand > after:
            return cand
    return _at(local_day + timedelta(days=3), t, tz)  # unreachable in practice


def band_window(
    now: datetime, start: time, end: time, tz: tzinfo,
) -> tuple[datetime, datetime] | None:
    """The fixed window [start, end) containing `now` (UTC instants), or None.

    The window may wrap midnight; start == end means no window."""
    if start == end:
        return None
    now = now.astimezone(timezone.utc)
    today = now.astimezone(tz).date()
    for day in (today, today - timedelta(days=1)):
        s = _at(day, start, tz)
        if s > now:
            continue
        e = _next_at(s, end, tz)
        if now < e:
            return s, e
    return None


def wake_after(
    window_start: datetime, wake: time | None, tz: tzinfo,
) -> datetime | None:
    """Next occurrence of the alarm after the band start (None = no alarm, or
    one too far from the night to be this night's wake)."""
    if wake is None:
        return None
    w = _next_at(window_start, wake, tz)
    if w - window_start > NIGHT_BAND_MAX_WAKE_SPAN:
        return None
    return w


def room_lead(
    recovery_min: float | None, confidence: float | None, max_lead: timedelta,
) -> timedelta:
    """Pre-heat lead for one room: the model's recovery time, clamped to
    [0, max_lead]; max_lead when the model is unsure or the room cannot reach
    its target (recovery None) — never an exploding lead (W3 lesson, 22/10)."""
    if (recovery_min is None or confidence is None
            or confidence < NIGHT_LEAD_MIN_CONFIDENCE):
        return max_lead
    return max(timedelta(0), min(max_lead, timedelta(minutes=recovery_min)))


@dataclass
class NightBandLatch:
    """Rooms whose pre-heat has started in the current window: they stay out
    of the band until it ends (no jump back in if the lead later shrinks)."""

    window_start: datetime | None = None
    started: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class NightBandPlan:
    status: str
    window_start: datetime | None = None
    window_end: datetime | None = None       # the fixed end (no alarm)
    wake: datetime | None = None             # the alarm used, if any
    room_end: dict[str, datetime] = field(default_factory=dict)
    trimmed: frozenset[str] = frozenset()
    reason: str | None = None


def night_band_plan(
    now: datetime,
    *,
    enabled: bool,
    active_mode: bool,
    start: time,
    end: time,
    tz: tzinfo,
    wake: time | None,
    leads: dict[str, timedelta],
    latch: NightBandLatch,
) -> tuple[NightBandPlan, NightBandLatch]:
    """Which rooms are in the night band right now.

    `active_mode` = house mode Casa AND Auto setback on (Via/Vacanza/#8/W3
    own their setpoints). `leads` maps each trimmable room to its pre-heat
    lead (only used with an alarm). The latch is returned updated; the caller
    keeps it only on an actuating pass if it wants, but it is clock-derived
    enough to be safe either way.
    """
    if not enabled:
        return NightBandPlan(status=BAND_OFF, reason="switch_spento"), NightBandLatch()
    window = band_window(now, start, end, tz)
    if window is None:
        return NightBandPlan(status=BAND_IDLE, reason="fuori_fascia"), NightBandLatch()
    w_start, w_end = window
    if latch.window_start != w_start:
        latch = NightBandLatch(window_start=w_start)
    wake_at = wake_after(w_start, wake, tz)
    if not active_mode:
        return NightBandPlan(
            status=BAND_IDLE, window_start=w_start, window_end=w_end, wake=wake_at,
            reason="modalita_non_casa",
        ), latch
    room_end: dict[str, datetime] = {}
    trimmed: set[str] = set()
    started = set(latch.started) if wake_at is not None else set()
    for zone, lead in leads.items():
        end_at = w_end
        if wake_at is not None:
            # The alarm can only END the band earlier (comfort at the wake),
            # never stretch it past the configured end.
            end_at = min(w_end, wake_at - lead)
            if zone in started:
                end_at = min(end_at, now)   # pre-heat started: no jump back in
        room_end[zone] = end_at
        if now < end_at:
            trimmed.add(zone)
        elif wake_at is not None and end_at < w_end:
            started.add(zone)
    if trimmed and len(trimmed) == len(leads):
        status, reason = BAND_ACTIVE, None
    elif wake_at is not None and now < wake_at:
        status, reason = BAND_PREHEAT, None
    else:
        status, reason = BAND_IDLE, "fine_fascia"
    plan = NightBandPlan(
        status=status, window_start=w_start, window_end=w_end, wake=wake_at,
        room_end=room_end, trimmed=frozenset(trimmed), reason=reason,
    )
    return plan, NightBandLatch(window_start=w_start, started=started)
