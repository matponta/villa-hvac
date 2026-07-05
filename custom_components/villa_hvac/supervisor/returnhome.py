"""Pure return-home pre-conditioning core (C2 split): the coarse ETA, the
advisory lead-time estimate, and the anti-chatter latch decision (#8)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta




# --- Story #8: return-home pre-conditioning (pure) ---------------------------
# While away (house_mode Via) with a return ETA armed, the house sits in deep
# setback (building_protection) and starts pre-conditioning `lead_time` before the
# ETA so it reaches comfort by arrival. The decision is SYMBOLIC ("waiting" /
# "precond" / None); the engine maps it onto an effective house mode (Vacanza /
# Casa) so the whole existing stack (house_mode_policy, FanBandController,
# precool_policy) follows with zero lever conflict. Pure so the schedule + the
# anti-chatter latch are fully unit-testable.

RETURN_WAITING = "waiting"    # deep setback until the pre-cond window opens

RETURN_PRECOND = "precond"    # ramp to comfort ahead of arrival



@dataclass(frozen=True)
class ReturnRoom:
    """Minimal per-room slice for the lead-time estimate (blended or prior model)."""

    temp: float | None
    target: float
    a: float
    b: float
    c: float
    k: float
    # S_eff (STORY_SEFF): this room's effective irradiance — b is per-room and
    # may be S_eff-fitted, so pairing it with the house GHI would mix units.
    # None falls back to the shared `solar` argument (GHI-identity rooms).
    s_eff: float | None = None



def return_eta(
    return_date, daypart: str | None, daypart_hours: dict[str, int], now: datetime
) -> datetime | None:
    """Compose the return ETA from a date + a coarse daypart -> a canonical hour.

    None when the date/daypart is missing or the daypart is unknown. The ETA
    carries `now`'s tzinfo; a past ETA is returned as-is (the caller decides).
    """
    if return_date is None or daypart is None:
        return None
    hour = daypart_hours.get(daypart)
    if hour is None:
        return None
    return datetime(
        return_date.year, return_date.month, return_date.day,
        int(hour), 0, 0, tzinfo=now.tzinfo,
    )



def return_lead_time(
    rooms: list[ReturnRoom], outdoor: float | None, solar: float | None,
    *, max_lead: timedelta, margin: timedelta,
    min_lead: timedelta = timedelta(minutes=15), rate_floor: float = 0.05,
) -> timedelta:
    """Lead time to bring the slowest cooled room from its current temp to its
    comfort target at full cooling.

    Advisory: uses the (blended or prior) model net rate
    k − a(T_out−target) − b·S − c, floored > 0 so a gain-limited room (net ≈ 0)
    clamps to `max_lead` (start as early as allowed; comfort at arrival is NOT
    guaranteed for the hardest rooms). Returns max-over-rooms + margin, clamped
    to [min_lead, max_lead].
    """
    o = outdoor if outdoor is not None else 0.0
    s_house = solar if solar is not None else 0.0
    worst = timedelta(0)
    for r in rooms:
        if r.temp is None:
            continue
        delta = r.temp - r.target
        if delta <= 0:
            continue
        s = r.s_eff if r.s_eff is not None else s_house
        rate = max(r.k - r.a * (o - r.target) - r.b * s - r.c, rate_floor)
        t = timedelta(hours=delta / rate)
        if t > worst:
            worst = t
    lead = worst + margin
    return max(min_lead, min(max_lead, lead))



def return_decision(
    *, is_via: bool, armed: bool, opt_in: bool,
    eta: datetime | None, lead_time: timedelta, now: datetime, latched: bool,
) -> tuple[str | None, bool]:
    """Symbolic away-return decision + the new latch.

    None -> #8 inert (normal Via behaviour). RETURN_WAITING -> deep setback.
    RETURN_PRECOND -> ramp to comfort. Latches on entry to the pre-cond window so
    a shrinking lead_time (as the rooms cool) can't un-trigger it (no chatter);
    the latch clears whenever #8 is inert (left Via / disarmed / opt-out).
    """
    if not (opt_in and is_via and armed and eta is not None):
        return None, False
    window_start = eta - lead_time
    if latched or now >= window_start:
        return RETURN_PRECOND, True
    return RETURN_WAITING, False
