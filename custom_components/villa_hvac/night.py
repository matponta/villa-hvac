"""Camere silenziose: night-time bedroom silence + heat-guard (#2b).

Embeds the legacy HA subsystem (buonanotte / sveglia / notte_guardia_caldo) for
the 2 bedrooms. When the house enters Notte the bedrooms go silent (fancoil to
`manuale` + fan off); a hysteresis heat-guard nudges the fan to a low stage if
the room overheats and silences it again once it cools. Leaving Notte or the
daily auto-wake restores AUTO so KNX resumes control.

`evaluate_guard` is pure (no HA) so the hysteresis is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging

from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    DOMAIN as FAN_DOMAIN,
    SERVICE_SET_PERCENTAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_AUTO_WAKE_TIME,
    DEFAULT_NIGHT_THRESHOLD,
    NIGHT_GUARD_FAN_PCT,
    NIGHT_GUARD_HIGH,
    NIGHT_GUARD_LOW,
    OPT_AUTO_WAKE_TIME,
    OPT_NIGHT_THRESHOLD,
    ZONES,
)
from .controller import auto_setback_enabled

_LOGGER = logging.getLogger(__name__)


def bedrooms() -> list[tuple[str, dict]]:
    """(zone_id, zone) for zones flagged as bedrooms."""
    return [(zid, z) for zid, z in ZONES.items() if z.get("bedroom")]


@dataclass(frozen=True)
class GuardState:
    """Per-bedroom heat-guard state."""

    cooling: bool = False
    above_since: datetime | None = None
    below_since: datetime | None = None


def evaluate_guard(
    state: GuardState,
    temp: float | None,
    threshold: float,
    now: datetime,
    high: timedelta = NIGHT_GUARD_HIGH,
    low: timedelta = NIGHT_GUARD_LOW,
) -> tuple[GuardState, str | None]:
    """Advance the hysteresis. Returns (new_state, action).

    action is "cool" (run the fan at the low stage), "silence" (fan off), or
    None (no change). Mirrors the legacy notte_guardia_caldo: above threshold
    for `high` -> cool; below threshold for `low` -> silence.
    """
    if temp is None:
        return state, None
    if not state.cooling:
        if temp > threshold:
            since = state.above_since or now
            if now - since >= high:
                return GuardState(cooling=True), "cool"
            return replace(state, above_since=since, below_since=None), None
        return replace(state, above_since=None), None
    # currently cooling
    if temp < threshold:
        since = state.below_since or now
        if now - since >= low:
            return GuardState(cooling=False), "silence"
        return replace(state, below_since=since, above_since=None), None
    return replace(state, below_since=None), None


class NightController:
    """Drives camere silenziose for the bedrooms, off the coordinator tick."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.active = False
        self._guards: dict[str, GuardState] = {zid: GuardState() for zid, _ in bedrooms()}
        self._unsub_coord = None
        self._unsub_wake = None
        self._busy = False

    def start(self) -> None:
        self._unsub_coord = self.coordinator.async_add_listener(self._on_coordinator_update)
        self._schedule_wake()

    def stop(self) -> None:
        if self._unsub_coord:
            self._unsub_coord()
        if self._unsub_wake:
            self._unsub_wake()

    def _threshold(self) -> float:
        try:
            return float(self.entry.options.get(OPT_NIGHT_THRESHOLD, DEFAULT_NIGHT_THRESHOLD))
        except (TypeError, ValueError):
            return DEFAULT_NIGHT_THRESHOLD

    async def enter(self) -> None:
        """Silence all bedrooms (manuale on + fan off)."""
        self.active = True
        for zid, zone in bedrooms():
            self._guards[zid] = GuardState()
            await self._switch(zone["manuale_switch"], on=True)
            await self._fan_off(zone["fancoils"][0])

    async def exit(self) -> None:
        """Wake all bedrooms (manuale off -> KNX resumes)."""
        self.active = False
        for zid, zone in bedrooms():
            self._guards[zid] = GuardState()
            await self._switch(zone["manuale_switch"], on=False)

    @callback
    def _on_coordinator_update(self) -> None:
        if not self.active or self._busy:
            return
        self.hass.async_create_task(self._run_guard())

    async def _run_guard(self) -> None:
        self._busy = True
        try:
            if not auto_setback_enabled(self.hass, self.entry):
                if self.active:
                    await self.exit()
                return
            threshold = self._threshold()
            now = dt_util.utcnow()
            zone_temps = self.coordinator.data.get("zone_temps") or {}
            for zid, zone in bedrooms():
                temp = (zone_temps.get(zid) or {}).get("value")
                new_state, action = evaluate_guard(
                    self._guards[zid], temp, threshold, now
                )
                self._guards[zid] = new_state
                if action == "cool":
                    await self._switch(zone["manuale_switch"], on=True)
                    await self._fan_pct(zone["fancoils"][0], NIGHT_GUARD_FAN_PCT)
                elif action == "silence":
                    await self._fan_off(zone["fancoils"][0])
        finally:
            self._busy = False

    def _schedule_wake(self) -> None:
        raw = str(self.entry.options.get(OPT_AUTO_WAKE_TIME, DEFAULT_AUTO_WAKE_TIME))
        parts = raw.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            second = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            hour, minute, second = 8, 0, 0
        self._unsub_wake = async_track_time_change(
            self.hass, self._on_wake, hour=hour, minute=minute, second=second
        )

    @callback
    def _on_wake(self, now: datetime) -> None:
        if self.active:
            self.hass.async_create_task(self.exit())

    async def _switch(self, entity_id: str, *, on: bool) -> None:
        await self.hass.services.async_call(
            "switch",
            SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    async def _fan_off(self, entity_id: str) -> None:
        await self.hass.services.async_call(
            FAN_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    async def _fan_pct(self, entity_id: str, pct: int) -> None:
        await self.hass.services.async_call(
            FAN_DOMAIN,
            SERVICE_SET_PERCENTAGE,
            {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: pct},
            blocking=True,
        )
