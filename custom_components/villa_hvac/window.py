"""Window pause (#4).

An open window/vasistas in a zone pauses that zone's cooling: after a short
debounce the thermostat is forced to building_protection (fan -> 0); when the
opening closes again the zone is restored to whatever the current house mode
dictates. Event-driven off the opening's state.

Cooperates with #2: a window-paused zone is recorded in `paused` and the
house-mode driver skips it, so a mode change won't silently resume cooling
while the window is still open. Respects #10 and the Auto setback switch.

Coverage is whatever zones carry a `window` entity in ZONES: the 3 bathroom
vasistas (covers) + since v0.55.0 the 6 Shelly BLU contacts on the main rooms
(binary_sensors; `on` = open is already in WINDOW_OPEN_STATES). The Porta
Cucina contact is mapped to the `living_room` LEADER — the kitchen has no
thermostat of its own (open space, one air volume with the Salotto).
BTHome contacts are battery/BLE: `unavailable` is deliberately ignored, so a
dead battery never pauses or un-pauses a room.

v0.57.0 (owner rule 3): a CONTACT open longer than OPT_WINDOW_ALERT_MINUTES
(default 30, 0 disables) pages Mattia + Ehi once per opening episode — unless
the house is deliberately airing (free_air on, or windows-free-cool armed with
enough contacts open).
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
    DEFAULT_WINDOW_ALERT_MINUTES,
    OPT_WINDOW_ALERT_MINUTES,
    OPT_WINDOW_ALERT_TARGETS,
    SEASON_SUMMER,
    WINDOW_ALERT_NAMES,
    WINDOW_ALERT_TAG,
    WINDOW_ALERT_TARGETS,
    WINDOW_CLOSED_STATES,
    WINDOW_OPEN_DELAY,
    WINDOW_OPEN_STATES,
    ZONES,
)
from .controller import (
    auto_setback_enabled,
    current_season,
    free_air_enabled,
    supervisor_enabled,
    windows_free_cool_enabled,
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
        # v0.57.0 long-open alert (owner rule 3): per-OPENING timers + a
        # once-per-episode latch. Contacts only (binary_sensor `window` keys) —
        # the bathroom vasistas never page anyone.
        self._alert_timers: dict[str, callable] = {}
        self._alerted: set[str] = set()
        self._unsub = None

    def start(self) -> None:
        openings = list(self._by_opening)
        if not openings:
            return
        self._unsub = async_track_state_change_event(
            self.hass, openings, self._on_change
        )
        # Catch a window that is already open at startup. The alert clock
        # restarts from boot (the true opening time is unknown across a restart).
        for opening, zone_id in self._by_opening.items():
            state = self.hass.states.get(opening)
            if state is not None and state.state in WINDOW_OPEN_STATES:
                self._schedule_pause(zone_id)
                self._schedule_alert(opening)

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        for cancel in self._alert_timers.values():
            cancel()
        self._alert_timers.clear()

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
            if opening not in self._alerted and opening not in self._alert_timers:
                self._schedule_alert(opening)
        elif state in WINDOW_CLOSED_STATES:
            self._cancel_timer(zone_id)
            self._cancel_alert(opening)
            self._alerted.discard(opening)  # a fresh opening may alert again
            if zone_id in self.paused:
                self.hass.async_create_task(self._restore(zone_id))
        # other states (unknown/unavailable) -> ignore (a BLE battery dying
        # mid-episode neither cancels nor fires the alert; a real close does)

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

    # -- v0.57.0 long-open alert (owner rule 3) --------------------------------
    def _alert_minutes(self) -> int:
        """Alert threshold in minutes (options-editable); 0 disables."""
        try:
            return max(0, int(float(self.entry.options.get(
                OPT_WINDOW_ALERT_MINUTES, DEFAULT_WINDOW_ALERT_MINUTES
            ))))
        except (TypeError, ValueError):
            return DEFAULT_WINDOW_ALERT_MINUTES

    def _alert_targets(self) -> list[str]:
        raw = self.entry.options.get(OPT_WINDOW_ALERT_TARGETS)
        if raw:
            return [t.strip() for t in str(raw).split(",") if t.strip()]
        return list(WINDOW_ALERT_TARGETS)

    def _cancel_alert(self, opening: str) -> None:
        cancel = self._alert_timers.pop(opening, None)
        if cancel:
            cancel()

    def _schedule_alert(self, opening: str) -> None:
        """Arm the long-open alert for a CONTACT (vasistas covers never page)."""
        minutes = self._alert_minutes()
        if minutes <= 0 or not opening.startswith("binary_sensor."):
            return
        self._cancel_alert(opening)
        self._alert_timers[opening] = async_call_later(
            self.hass, minutes * 60, self._alert_cb(opening)
        )

    def _alert_cb(self, opening: str):
        @callback
        def _fire(_now) -> None:
            self._alert_timers.pop(opening, None)
            self.hass.async_create_task(self._alert(opening))

        return _fire

    def _deliberately_airing(self) -> bool:
        """True while the open windows are on purpose — no paging then.

        free_air = the manual windows-open switch; windows-free-cool = the
        opt-in inference with enough contacts open, SUMMER only (the switch is
        a summer-coast opt-in; three windows open in January are not deliberate
        airing, and heating into them is exactly what rule 3 must catch). The
        temperature half of the verdict is deliberately NOT required —
        deliberateness is intent, not thermodynamics. A suppressed alert is
        RE-ARMED, never consumed (see `_alert`).
        """
        if free_air_enabled(self.hass, self.entry):
            return True
        if not windows_free_cool_enabled(self.hass, self.entry):
            return False
        if current_season(self.hass, self.entry) != SEASON_SUMMER:
            return False
        cfg_count = self._windows_free_cool_count()
        open_count = sum(
            1 for opening in self._by_opening
            if opening.startswith("binary_sensor.")
            and (s := self.hass.states.get(opening)) is not None
            and s.state in WINDOW_OPEN_STATES
        )
        return open_count >= cfg_count

    def _windows_free_cool_count(self) -> int:
        from .supervisor_config import SupervisorConfig

        return SupervisorConfig.from_options(self.entry.options).windows_free_cool_count

    async def _alert(self, opening: str) -> None:
        """One page per opening episode, to every configured target; guarded so
        one missing phone can't block the other.

        A suppressed or undecidable fire RE-ARMS the timer instead of consuming
        the episode (adversarial review): the cleaning-day case — six windows
        open (suppressed while airing), five closed, ONE forgotten — is exactly
        what rule 3 exists for, and a consumed episode would never page it.
        """
        state = self.hass.states.get(opening)
        if state is not None and state.state in WINDOW_CLOSED_STATES:
            return  # genuinely closed while the timer was in flight
        if state is None or state.state not in WINDOW_OPEN_STATES:
            # unavailable/unknown (BLE blip) at fire time: neither page nor
            # drop the episode — check again in one interval.
            self._schedule_alert(opening)
            return
        if self._deliberately_airing():
            self._schedule_alert(opening)  # re-check once the airing ends
            return
        self._alerted.add(opening)
        zone_id = self._by_opening.get(opening)
        name = WINDOW_ALERT_NAMES.get(
            opening, ZONES.get(zone_id, {}).get("name", zone_id or opening)
        )
        minutes = self._alert_minutes()
        # Claim the pause only when it is actually ENGAGED — with the master or
        # Auto-setback off the room is NOT paused, and a message asserting it
        # is would send the reader away from an open window feeding the AC.
        paused = (
            zone_id in self.paused
            and supervisor_enabled(self.hass, self.entry)
            and auto_setback_enabled(self.hass, self.entry)
        )
        message = f"{name}: finestra aperta da {minutes} minuti"
        message += (
            " — il clima della stanza è in pausa." if paused
            else " — ATTENZIONE: il clima NON è in pausa (supervisor/setback off)."
            if zone_id in ZONES and ZONES[zone_id].get("climate")
            else "."
        )
        for target in self._alert_targets():
            service = target.removeprefix("notify.")
            try:
                await self.hass.services.async_call(
                    "notify", service,
                    {
                        "title": "Finestra aperta",
                        "message": message,
                        "data": {"tag": f"{WINDOW_ALERT_TAG}_{zone_id}"},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - paging is best-effort, never fatal
                _LOGGER.warning(
                    "Window alert: could not notify %s for %s", target, opening,
                    exc_info=True,
                )

    async def _request_run(self) -> None:
        engine = getattr(self.entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
