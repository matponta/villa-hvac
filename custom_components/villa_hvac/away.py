"""Away auto-escalation (#2c).

Replaces the legacy automation.clima_backup_via_quando_esco: after the adults
are away for a configurable spell (while the house is in Casa or Notte), drop to
Via; when they return and the house is still in the *auto-set* Via, restore Casa.
Restore only triggers from Via, so it never overrides a manual Notte/Vacanza.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_HOME, STATE_NOT_HOME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    DEFAULT_AWAY_HOURS,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_NIGHT,
    OPT_AWAY_HOURS,
    PRESENCE_GROUP,
)
from .controller import auto_setback_enabled, current_house_mode

_LOGGER = logging.getLogger(__name__)


def escalation_target(current_mode: str) -> str | None:
    """Mode to switch to after a long absence (None = no change)."""
    if current_mode in (HOUSE_MODE_HOME, HOUSE_MODE_NIGHT):
        return HOUSE_MODE_AWAY
    return None


def restore_target(current_mode: str) -> str | None:
    """Mode to restore when presence returns (None = no change).

    Only the auto-set Via is undone — a manual Notte/Vacanza is left alone.
    """
    if current_mode == HOUSE_MODE_AWAY:
        return HOUSE_MODE_HOME
    return None


class AwayController:
    """Escalates to Via on long absence and restores Casa on return."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub_state = None
        self._cancel_timer = None

    def start(self) -> None:
        self._unsub_state = async_track_state_change_event(
            self.hass, [PRESENCE_GROUP], self._on_presence
        )
        state = self.hass.states.get(PRESENCE_GROUP)
        if state is not None and state.state == STATE_NOT_HOME:
            self._schedule()

    def stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
        self._clear_timer()

    def _clear_timer(self) -> None:
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

    def _delay(self) -> timedelta:
        try:
            hours = float(self.entry.options.get(OPT_AWAY_HOURS, DEFAULT_AWAY_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_AWAY_HOURS
        return timedelta(hours=hours)

    @callback
    def _on_presence(self, event: Event) -> None:
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        old = old_state.state if old_state else None
        new = new_state.state if new_state else None
        if new == old:
            return  # attribute-only churn: don't reset the absence timer
        if new == STATE_NOT_HOME:
            self._schedule()
        elif new == STATE_HOME:
            self._clear_timer()
            self.hass.async_create_task(self._restore())

    def _schedule(self) -> None:
        self._clear_timer()
        self._cancel_timer = async_call_later(
            self.hass, self._delay().total_seconds(), self._escalate_cb
        )

    @callback
    def _escalate_cb(self, _now) -> None:
        self._cancel_timer = None
        self.hass.async_create_task(self._escalate())

    async def _escalate(self) -> None:
        if not auto_setback_enabled(self.hass, self.entry):
            return
        state = self.hass.states.get(PRESENCE_GROUP)
        if state is None or state.state != STATE_NOT_HOME:
            return  # came back before the timer fired
        target = escalation_target(current_house_mode(self.hass, self.entry))
        if target:
            await self._set_mode(target)

    async def _restore(self) -> None:
        if not auto_setback_enabled(self.hass, self.entry):
            return
        target = restore_target(current_house_mode(self.hass, self.entry))
        if target:
            await self._set_mode(target)

    async def _set_mode(self, mode: str) -> None:
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "select", DOMAIN, f"{self.entry.entry_id}_house_mode"
        )
        if not entity_id:
            return
        await self.hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: entity_id, "option": mode},
            blocking=True,
        )
