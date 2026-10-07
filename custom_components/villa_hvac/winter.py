"""Winter fancoils OFF (v0.79.0, owner rule 2026-10-07).

In winter the villa heats ONLY through the radiant floor, regulated by the KNX
thermostats. The fancoils must stay OFF: every fancoil fan's ON/OFF switch
object is held off through the arbiter (`fan_power:` lever — re-asserted, a
hand press concedes for the override backoff, then off again). With the fan
OFF the KNX interlock also holds the EV FAN valve closed.

Why a dedicated lever: after the 2026-10-07 changeover every fan sat ON at 0 %
in AUTO; the `fan:` % lever reads that as "0" = already satisfied and could
never assert a real OFF.

Hand-back: on the first non-winter cycle after managing, one explicit ON per
fan (KNX AUTO won't restart a fan whose switch object was written OFF — the
dead-fan-at-wake fact). After a restart in summer the in-memory flag is gone;
the summer stranded-fan watchdog revives any fan still off once its room calls.
"""
from __future__ import annotations

from .const import (
    DEAD_FANCOILS,
    SEASON_WINTER,
    UNIT_FANS,
    WINTER_PRESET,
    WINTER_SETPOINT_MAX,
    WINTER_SETPOINT_MIN,
)
from .supervisor import fan_power_lever, preset_lever, temperature_lever
from .supervisor.winter_plan import PvHeatState, pv_surplus_step

# The dead sala-giochi unit is left out: its state never follows a write, so it
# would only cycle write → re-assert → concede every backoff.
WINTER_FANS: tuple[str, ...] = tuple(sorted(set(UNIT_FANS.values()) - DEAD_FANCOILS))
# Leaving winter, the ON hand-back is asserted this many cycles (the arbiter
# re-asserts a dropped telegram within the window) — not a one-shot.
HANDBACK_CYCLES = 10


class WinterFancoilController:
    """Merge controller: winter → every fancoil fan OFF; leaving winter → ON once."""

    def __init__(self) -> None:
        self._handback = 0

    def __call__(self, state) -> dict:
        if state.season == SEASON_WINTER:
            self._handback = HANDBACK_CYCLES
            return {fan_power_lever(f): "off" for f in WINTER_FANS}
        if self._handback > 0:
            self._handback -= 1
            return {fan_power_lever(f): "on" for f in WINTER_FANS}
        return {}


class PvHeatController:
    """v0.81.0 W4 — heat chosen rooms from a real Condominio PV surplus.

    Owner rule 2026-10-07: on surplus (battery ≥ 90 % AND a net outflow to the
    grid / into the battery — Condominio NET flows, so another apartment running
    the shared PdC removes the surplus by itself) the selected rooms go to their
    Casa target even in Via / Notte / Vacanza: the radiant slab banks free heat.
    Setpoint-only; lifts building_protection → comfort for those rooms only while
    active. Never lowers (no opinion when the mode already asks ≥ Casa), skips
    #10-disabled / #4-paused rooms (controllers merge above those policies).
    """

    def __init__(self) -> None:
        self.state = PvHeatState()
        self.rooms: tuple[str, ...] = ()

    def __call__(self, state) -> dict:
        # auto_setback gate (review MAJOR): with it off nothing re-asserts the
        # mode setpoint after the surplus ends — the lift would stay for good.
        if (
            state.season != SEASON_WINTER or not state.pv_heat_enabled
            or not state.auto_setback
        ):
            self.state = PvHeatState(reason="off")
            self.rooms = ()
            return {}
        sun_up = state.sun_elevation is not None and state.sun_elevation > 0
        self.state = pv_surplus_step(
            self.state, now=state.now, soc=state.condo_soc,
            grid_w=state.condo_grid_w, battery_w=state.condo_battery_w,
            sun_up=sun_up,
        )
        if not self.state.active or state.house_setpoint is None:
            self.rooms = ()
            return {}
        out: dict = {}
        rooms = []
        for z in state.zones.values():
            if z.zone_id not in state.pv_heat_zones or not z.climate:
                continue
            if not z.enabled or z.paused:
                continue
            casa = state.house_setpoint + z.setpoint_offset
            mode = (
                state.house_setpoint + state.mode_offset + z.setpoint_offset
                if state.mode_offset is not None else None
            )
            if mode is not None and mode >= casa:
                continue  # the house mode already asks for Casa (or more)
            out[preset_lever(z.climate)] = WINTER_PRESET
            out[temperature_lever(z.climate)] = round(
                max(WINTER_SETPOINT_MIN, min(WINTER_SETPOINT_MAX, casa)), 1
            )
            rooms.append(z.zone_id)
        self.rooms = tuple(rooms)
        return out
