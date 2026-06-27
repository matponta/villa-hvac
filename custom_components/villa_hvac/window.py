"""Window pause (#4).

An open window/vasistas in a zone pauses that zone's cooling: after a short
debounce the thermostat is forced to building_protection (fan -> 0); when the
opening closes again the zone is restored to whatever the current house mode
dictates. Event-driven off the opening's state.

Cooperates with #2: a window-paused zone is recorded in `paused` and the
house-mode driver skips it, so a mode change won't silently resume cooling
while the window is still open. Respects #10 and the Auto setback switch.

Coverage is whatever zones carry a `window` entity in ZONES (only gabriroom
today); the mechanism is generic so more can be added as sensors are fitted.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    WINDOW_CLOSED_STATES,
    WINDOW_OPEN_DELAY,
    WINDOW_OPEN_STATES,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


def window_zones() -> list[tuple[str, str]]:
    """(zone_id, opening_entity) for zones with a window mapped."""
    return [(zid, z["window"]) for zid, z in ZONES.items() if z.get("window")]


class WindowController:
    """Pauses a zone's cooling while its window is open."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._by_opening = {opening: zid for zid, opening in window_zones()}
        self.paused: set[str] = set()
        self._timers: dict[str, callable] = {}
        self._unsub = None

    def start(self) -> None:
        openings = list(self._by_opening)
        if not openings:
            return
        self._unsub = async_track_state_change_event(
            self.hass, openings, self._on_change
        )
        # Catch a window that is already open at startup.
        for opening, zone_id in self._by_opening.items():
            state = self.hass.states.get(opening)
            if state is not None and state.state in WINDOW_OPEN_STATES:
                self._schedule_pause(zone_id)

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    @callback
    def _on_change(self, event: Event) -> None:
        opening = event.data["entity_id"]
        zone_id = self._by_opening.get(opening)
        if zone_id is None:
            return
        new_state = event.data.get("new_state")
        state = new_state.state if new_state else None
        if state in WINDOW_OPEN_STATES:
            if zone_id not in self.paused and zone_id not in self._timers:
                self._schedule_pause(zone_id)
        elif state in WINDOW_CLOSED_STATES:
            self._cancel_timer(zone_id)
            if zone_id in self.paused:
                self.hass.async_create_task(self._restore(zone_id))
        # other states (unknown/unavailable) -> ignore

    def _cancel_timer(self, zone_id: str) -> None:
        cancel = self._timers.pop(zone_id, None)
        if cancel:
            cancel()

    def _schedule_pause(self, zone_id: str) -> None:
        self._cancel_timer(zone_id)
        self._timers[zone_id] = async_call_later(
            self.hass, WINDOW_OPEN_DELAY.total_seconds(), self._pause_cb(zone_id)
        )

    def _pause_cb(self, zone_id: str):
        @callback
        def _fire(_now) -> None:
            self._timers.pop(zone_id, None)
            self.hass.async_create_task(self._pause(zone_id))

        return _fire

    async def _pause(self, zone_id: str) -> None:
        """Mark the zone paused and let the engine apply it.

        Actuation is the engine's `window_pause_policy` (building_protection for
        a paused, still-enabled zone); here we only flip the flag the policy
        reads and nudge an immediate pass. No-op write while the master is off.
        """
        self.paused.add(zone_id)
        await self._request_run()

    async def _restore(self, zone_id: str) -> None:
        self.paused.discard(zone_id)
        await self._request_run()

    async def _request_run(self) -> None:
        engine = getattr(self.entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
