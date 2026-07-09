"""Away auto-escalation (#2c).

Replaces the legacy automation.clima_backup_via_quando_esco: after the adults
are away for a configurable spell (while the house is in Casa or Notte), drop to
Via; when they return and the house is still in the *auto-set* Via, restore Casa.
Restore only triggers from Via, so it never overrides a manual Notte/Vacanza.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_HOME,
    STATE_NOT_HOME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
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
    PRESENCE_PERSONS,
)
from .controller import auto_setback_enabled, current_house_mode

_LOGGER = logging.getLogger(__name__)


def aggregate_presence(states: Iterable[str | None]) -> str | None:
    """Fuse the adult `person.*` states into one presence signal.

    home if ANY adult is home; not_home if at least one adult has a known state
    and none is home; None if every adult is unknown/unavailable/missing (can't
    tell — don't act). A person parked in a named zone (e.g. "work") is NOT home.
    """
    known = [s for s in states if s not in (None, STATE_UNKNOWN, STATE_UNAVAILABLE)]
    if not known:
        return None
    if any(s == STATE_HOME for s in known):
        return STATE_HOME
    return STATE_NOT_HOME


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
        self._last_presence: str | None = None

    def _presence(self) -> str | None:
        """Current fused presence over the adult person entities."""
        states = []
        for person in PRESENCE_PERSONS:
            st = self.hass.states.get(person)
            states.append(st.state if st is not None else None)
        return aggregate_presence(states)

    def start(self) -> None:
        self._unsub_state = async_track_state_change_event(
            self.hass, list(PRESENCE_PERSONS), self._on_presence
        )
        self._last_presence = self._presence()
        if self._last_presence == STATE_NOT_HOME:
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
        # Any adult moved: recompute the FUSED presence and act only on a real
        # aggregate transition (one adult leaving while the other stays home is
        # not a house-empty event).
        presence = self._presence()
        if presence is None or presence == self._last_presence:
            return  # all-unknown, or attribute-only churn: don't reset the timer
        self._last_presence = presence
        if presence == STATE_NOT_HOME:
            self._schedule()
        elif presence == STATE_HOME:
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
        if self._presence() != STATE_NOT_HOME:
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
