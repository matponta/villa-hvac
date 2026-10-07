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

from .const import SEASON_WINTER, UNIT_FANS
from .supervisor import fan_power_lever

WINTER_FANS: tuple[str, ...] = tuple(sorted(set(UNIT_FANS.values())))


class WinterFancoilController:
    """Merge controller: winter → every fancoil fan OFF; leaving winter → ON once."""

    def __init__(self) -> None:
        self._managing = False

    def __call__(self, state) -> dict:
        if state.season == SEASON_WINTER:
            self._managing = True
            return {fan_power_lever(f): "off" for f in WINTER_FANS}
        if self._managing:
            self._managing = False
            return {fan_power_lever(f): "on" for f in WINTER_FANS}
        return {}
