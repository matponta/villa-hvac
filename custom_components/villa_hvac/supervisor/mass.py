"""Mass-aware summer control (v0.74.0, system review 2026-09-30 §4). Pure.

Two opt-in mechanisms, both born from the live 2026 statistics:

1. MASS MAINTENANCE (Via, summer). The rooms' envelope time constants are
   3-7 days, so a deep setback charges the thermal mass with heat and the
   return costs a whole day of compressor: 9/8, 20/8, 30/8 and 9/9 were 18-24 h
   recovery days (9/9: 23 h on a 27 °C day, 36 h continuous after a 20 h Via).
   With the switch on, Via caps its offset at a mild value (bank coolth in the
   cool hours) EXCEPT while the outdoor is at/above the peak threshold, where
   the full offset applies (coast through the peak — cooling there only offsets
   solar gain, measured ~0 net). Hysteresis on the peak edge.

2. DEMAND SHEDDING. The PdC call is the OR of the room valves, so ONE slow room
   holds the whole compressor on (9/9: every room flat at setpoint, the office
   alone still descending). When the compressor AND each caller valve have run
   >= SHED_MIN_RUN for at most `max_callers` rooms that are already inside the
   comfort ceiling, lift
   their setpoint just above their temperature: the valve closes, the PdC
   rests, the room floats. Released when it reaches the comfort ceiling, when
   ANOTHER room calls (the compressor is running anyway → ride along = natural
   sync), after SHED_MAX_HOLD, or when the room stops being eligible. A released
   room cannot be shed again for SHED_REARM (anti short-cycle).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .arbiter import temperature_lever
from .model import HouseState, _is_free_cooling, active_cooling_leaders

SEASON_SUMMER = "summer"          # mirrors const.SEASON_SUMMER (import-pure)
HOUSE_MODE_AWAY = "Via"
MASS_PEAK_HYSTERESIS = 1.0        # °C below the peak threshold to leave "coast"

SHED_MIN_RUN = timedelta(minutes=20)
SHED_MIN_HOLD = timedelta(minutes=10)
SHED_MAX_HOLD = timedelta(minutes=90)
SHED_REARM = timedelta(minutes=30)
SHED_COMFORT_MARGIN = 0.3         # start only this far below the comfort ceiling
SHED_SETPOINT_LIFT = 0.5          # setpoint = temp + lift (valve closes)


# --- 1. mass maintenance ---------------------------------------------------------

def mass_via_offset(
    *, mode_offset: float, outdoor: float | None, peak: float, mild: float,
    coasting: bool,
) -> tuple[float, bool]:
    """(effective Via offset, coasting) for one cycle. Unknown outdoor keeps the
    previous phase (and coasts when there is none) — the full setback is the
    conservative direction for an empty house."""
    if outdoor is not None:
        if coasting:
            coasting = outdoor >= peak - MASS_PEAK_HYSTERESIS
        else:
            coasting = outdoor >= peak
    if coasting:
        return mode_offset, True
    return min(mode_offset, mild), False


# --- 2. demand shedding -----------------------------------------------------------

@dataclass
class ShedState:
    run_since: datetime | None = None           # consenso continuously ON since
    open_since: dict[str, datetime] = field(default_factory=dict)  # caller valve
    shed_since: dict[str, datetime] = field(default_factory=dict)
    rearm_until: dict[str, datetime] = field(default_factory=dict)
    last_reason: dict[str, str] = field(default_factory=dict)
    # climate -> the base setpoint recorded when the room was lifted (the
    # fail-safe restores it; live reads are gone on the unload path).
    snapshot: dict[str, float] = field(default_factory=dict)


def _unit_demand(state: HouseState, zone_id: str) -> bool:
    """A leader calls if its own valve or a follower's (open-space) is OPEN."""
    z = state.zones[zone_id]
    if z.demand:
        return True
    return any(f.follows == zone_id and f.demand for f in state.zones.values())


def _base_target(state: HouseState, z) -> float | None:
    if state.house_setpoint is None or state.mode_offset is None:
        return None
    return round(state.house_setpoint + state.mode_offset + z.setpoint_offset, 1)


class DemandShedController:
    """Merge controller (placed after the other controllers: guards, the
    governor and #2b always win their levers). Emits temperature opinions only,
    for the rooms it is shedding; no opinion = house_mode re-asserts the base."""

    def __init__(self) -> None:
        self.state = ShedState()

    def __call__(self, state: HouseState) -> dict:
        st = self.state
        now = state.now
        if (
            not state.demand_shedding_enabled
            or state.season != SEASON_SUMMER
            or state.mode_offset is None
            or state.house_setpoint is None
            or state.duty_comfort_max is None
            or _is_free_cooling(state)
        ):
            self._release_all(now, "disabilitato / non applicabile")
            st.run_since = None
            return {}
        consenso_on = state.consenso_freddo == "on"
        if consenso_on:
            if st.run_since is None:
                st.run_since = now
        else:
            st.run_since = None
        eligible = {
            z.zone_id: z for z in active_cooling_leaders(state)
            if not z.manuale_on and z.temp is not None
        }
        ceiling = state.duty_comfort_max
        callers = {
            zid for zid, z in state.zones.items()
            if z.demand and z.follows is None
        } | {
            z.follows for z in state.zones.values() if z.follows and z.demand
        }
        # Each caller's own continuous call time: a room that JUST started
        # calling is never rested (it would short-cycle the compressor).
        for zid in callers:
            st.open_since.setdefault(zid, now)
        for zid in list(st.open_since):
            if zid not in callers:
                st.open_since.pop(zid)
        # -- release -------------------------------------------------------
        for zid in list(st.shed_since):
            z = eligible.get(zid)
            held = now - st.shed_since[zid]
            others = callers - set(st.shed_since)
            reason = None
            if z is None:
                reason = "stanza non più idonea"
            elif z.temp >= ceiling:
                reason = "raggiunto il limite di comfort"
            elif others and held >= SHED_MIN_HOLD:
                reason = "un'altra stanza chiama: riparte insieme"
            elif held >= SHED_MAX_HOLD:
                reason = "durata massima"
            if reason is not None:
                self._release(zid, now, reason)
        # -- start ---------------------------------------------------------
        if (
            consenso_on and not st.shed_since
            and st.run_since is not None and now - st.run_since >= SHED_MIN_RUN
            and callers
            and len(callers) <= state.config_shed_max_callers
            and callers <= set(eligible)
        ):
            ok = all(
                eligible[zid].temp <= ceiling - SHED_COMFORT_MARGIN
                and st.rearm_until.get(zid, now) <= now
                and now - st.open_since[zid] >= SHED_MIN_RUN
                for zid in callers
            )
            if ok:
                for zid in callers:
                    st.shed_since[zid] = now
                    st.last_reason[zid] = (
                        "unica stanza a tenere acceso il PdC, già entro il comfort"
                    )
        # -- emit ----------------------------------------------------------
        out: dict = {}
        for zid in st.shed_since:
            z = eligible.get(zid)
            base = _base_target(state, z) if z is not None else None
            if z is None or base is None or z.climate is None:
                continue
            target = min(ceiling, round(z.temp + SHED_SETPOINT_LIFT, 1))
            out[temperature_lever(z.climate)] = max(base, target)
            st.snapshot[z.climate] = base
        # Snapshots of rooms no longer lifted are handed back by house_mode.
        live = {state.zones[zid].climate for zid in st.shed_since if zid in state.zones}
        for climate in list(st.snapshot):
            if climate not in live:
                st.snapshot.pop(climate)
        return out

    def _release(self, zid: str, now: datetime, reason: str) -> None:
        self.state.shed_since.pop(zid, None)
        self.state.rearm_until[zid] = now + SHED_REARM
        self.state.last_reason[zid] = reason

    def _release_all(self, now: datetime, reason: str) -> None:
        for zid in list(self.state.shed_since):
            self._release(zid, now, reason)

    def failsafe_setpoints(self) -> dict[str, float]:
        """Hand-back targets for the engine fail-safe: the base setpoint of every
        room currently lifted. Clears the shed state (nothing re-asserts)."""
        out = dict(self.state.snapshot)
        self.state = ShedState()
        return out

    def view(self) -> dict:
        st = self.state
        return {
            "shedding": sorted(st.shed_since),
            "since": {z: t.isoformat() for z, t in st.shed_since.items()},
            "rearm_until": {z: t.isoformat() for z, t in st.rearm_until.items()},
            "reasons": dict(st.last_reason),
            "run_since": st.run_since.isoformat() if st.run_since else None,
        }


def apply_mass_maintenance(state: HouseState, coasting: bool, *, mild: float,
                           peak: float) -> tuple[HouseState, bool, str | None]:
    """Engine seam: rewrite the effective Via offset. Returns (state, coasting,
    phase) where phase is "bank" / "coast" / None (not applicable)."""
    if (
        not state.mass_maintenance_enabled
        or state.season != SEASON_SUMMER
        or state.house_mode != HOUSE_MODE_AWAY
        or state.mode_offset is None
    ):
        return state, False, None
    offset, coasting = mass_via_offset(
        mode_offset=state.mode_offset, outdoor=state.outdoor_temp,
        peak=peak, mild=mild, coasting=coasting,
    )
    phase = "coast" if coasting else "bank"
    return replace(state, mode_offset=offset, mass_phase=phase), coasting, phase
