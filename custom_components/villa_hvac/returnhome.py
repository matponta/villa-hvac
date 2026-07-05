"""Story #8 — return-home pre-conditioning (HA wiring).

Two pieces:

* `AwayReturnController` — stateful. Each cycle the engine calls `apply(state)`,
  which reads the armed return ETA + the opt-in and, while `house_mode == Via`,
  overrides the EFFECTIVE house mode on the HouseState: `Vacanza`
  (building_protection, deep setback) while waiting, `Casa` (comfort ramp) once
  inside the pre-cond window. The whole existing policy stack then follows with no
  lever conflict. Holds the anti-chatter latch (advances only when the engine is
  actuating). The pure decision/lead-time/eta live in `supervisor.py`.

* `ReturnHomeManager` — fires the actionable "when are you back?" notification on
  the Via transition and maps the tapped action back onto the entities.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    COOL_GAIN_SOLAR,
    DEFAULT_RETURN_DAYPART_HOURS,
    DEFAULT_RETURN_MARGIN_MIN,
    DEFAULT_RETURN_MAX_LEAD_HOURS,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_VACATION,
    OPT_NOTIFY_TARGET,
    OPT_RETURN_MARGIN_MIN,
    OPT_RETURN_MAX_LEAD_HOURS,
    RETURN_ACTION_PREFIX,
    RETURN_ACTION_UNKNOWN,
    RETURN_DAYPART_EVENING,
    RETURN_DAYPART_MORNING,
    RETURN_NOTIFY_TAG,
)
from .controller import (
    return_armed,
    return_date,
    return_daypart,
    return_precond_enabled,
)
from .supervisor import (
    RETURN_PRECOND,
    RETURN_WAITING,
    ReturnRoom,
    return_decision,
    return_eta,
    return_lead_time,
)

_LOGGER = logging.getLogger(__name__)


def _opt_float(entry: ConfigEntry, key: str, default: float) -> float:
    try:
        return float(entry.options.get(key, default))
    except (TypeError, ValueError):
        return default


class AwayReturnController:
    """Overrides the effective house mode while Via+armed (#8). Holds the latch."""

    def __init__(self) -> None:
        self._latched = False
        # Last computed view, for the diagnostic sensor (read-only).
        self.decision: str | None = None
        self.eta = None
        self.lead: timedelta | None = None

    def _rooms(self, state, target: float) -> list[ReturnRoom]:
        rooms: list[ReturnRoom] = []
        for z in state.zones.values():
            if not (z.climate and z.emitter == "fancoil" and not z.follows):
                continue
            if not z.enabled or z.paused:
                continue
            rooms.append(
                ReturnRoom(
                    temp=z.temp, target=target,
                    a=z.model_a if z.model_a is not None else COOL_GAIN_OUTDOOR,
                    b=z.model_b if z.model_b is not None else COOL_GAIN_SOLAR,
                    c=z.model_c if z.model_c is not None else COOL_GAIN_BASE,
                    k=z.model_k if (z.model_k and z.model_k > 0) else COOL_CAPACITY,
                    s_eff=z.s_eff,
                )
            )
        return rooms

    def apply(
        self, state, hass: HomeAssistant, entry: ConfigEntry, *, commit: bool
    ):
        """Return `state`, possibly with house_mode/mode_offset overridden by #8.

        `commit` (= the engine is actuating) gates advancing the latch, mirroring
        how the duty/pacing timers only move on an actuating pass.
        """
        opt_in = return_precond_enabled(hass, entry)
        armed = return_armed(hass, entry)
        eta = return_eta(
            return_date(hass, entry), return_daypart(hass, entry),
            DEFAULT_RETURN_DAYPART_HOURS, state.now,
        )
        # Comfort target = the Casa setpoint (offset 0). Without it we can't size
        # the lead or the ramp -> stay inert.
        target = state.house_setpoint
        is_via = state.house_mode == HOUSE_MODE_AWAY
        if target is None:
            lead = timedelta(0)
        else:
            lead = return_lead_time(
                self._rooms(state, target), state.outdoor_temp, state.solar,
                max_lead=timedelta(
                    hours=_opt_float(
                        entry, OPT_RETURN_MAX_LEAD_HOURS, DEFAULT_RETURN_MAX_LEAD_HOURS
                    )
                ),
                margin=timedelta(
                    minutes=_opt_float(
                        entry, OPT_RETURN_MARGIN_MIN, DEFAULT_RETURN_MARGIN_MIN
                    )
                ),
            )
        decision, new_latched = return_decision(
            is_via=is_via and target is not None, armed=armed, opt_in=opt_in,
            eta=eta, lead_time=lead, now=state.now, latched=self._latched,
        )
        if commit:
            self._latched = new_latched
        self.decision, self.eta, self.lead = decision, eta, lead
        if decision == RETURN_WAITING:
            return replace(state, house_mode=HOUSE_MODE_VACATION, mode_offset=None)
        if decision == RETURN_PRECOND:
            return replace(state, house_mode=HOUSE_MODE_HOME, mode_offset=0.0)
        return state


# --- Actionable-notification trigger -----------------------------------------

# action id suffix -> (day offset, daypart). UNKNOWN disarms.
_ACTIONS: dict[str, tuple[int, str]] = {
    "TODAY_EVENING": (0, RETURN_DAYPART_EVENING),
    "TOMORROW_MORNING": (1, RETURN_DAYPART_MORNING),
    "TOMORROW_EVENING": (1, RETURN_DAYPART_EVENING),
}


class ReturnHomeManager:
    """Asks 'when are you back?' on entering Via and applies the tapped answer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub_mode = None
        self._unsub_action = None

    def start(self) -> None:
        mode_id = self._entity_id("select", "house_mode")
        if mode_id:
            self._unsub_mode = async_track_state_change_event(
                self.hass, [mode_id], self._on_mode
            )
        self._unsub_action = self.hass.bus.async_listen(
            "mobile_app_notification_action", self._on_action
        )

    def stop(self) -> None:
        if self._unsub_mode:
            self._unsub_mode()
        if self._unsub_action:
            self._unsub_action()

    def _entity_id(self, domain: str, suffix: str) -> str | None:
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            domain, DOMAIN, f"{self.entry.entry_id}_{suffix}"
        )

    @callback
    def _on_mode(self, event: Event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None or new.state != HOUSE_MODE_AWAY:
            return
        if old is not None and old.state == HOUSE_MODE_AWAY:
            return  # already Via -> attribute churn, ask only on the transition
        if not return_precond_enabled(self.hass, self.entry):
            return
        if return_armed(self.hass, self.entry):
            return  # already told it when we're back
        self.hass.async_create_task(self._ask())

    def _notify_service(self) -> str | None:
        target = self.entry.options.get(OPT_NOTIFY_TARGET)
        if target:
            return target
        for service in self.hass.services.async_services().get("notify", {}):
            if service.startswith("mobile_app_"):
                return service
        return None

    async def _ask(self) -> None:
        service = self._notify_service()
        if not service:
            _LOGGER.debug("Return-home: no notify.mobile_app_* target; skipping ask")
            return
        actions = [
            {"action": f"{RETURN_ACTION_PREFIX}TODAY_EVENING", "title": "Stasera"},
            {"action": f"{RETURN_ACTION_PREFIX}TOMORROW_MORNING", "title": "Domani mattino"},
            {"action": f"{RETURN_ACTION_PREFIX}TOMORROW_EVENING", "title": "Domani sera"},
            {"action": RETURN_ACTION_UNKNOWN, "title": "Non so"},
        ]
        await self.hass.services.async_call(
            "notify", service,
            {
                "title": "Casa in pausa",
                "message": "Quando torni? Pre-condiziono la villa per il tuo arrivo.",
                "data": {"tag": RETURN_NOTIFY_TAG, "actions": actions},
            },
            blocking=False,
        )

    @callback
    def _on_action(self, event: Event) -> None:
        action = event.data.get("action")
        if not action:
            return
        if action == RETURN_ACTION_UNKNOWN:
            self.hass.async_create_task(self._set_armed(False))
            return
        if not action.startswith(RETURN_ACTION_PREFIX):
            return
        mapping = _ACTIONS.get(action[len(RETURN_ACTION_PREFIX):])
        if mapping is None:
            return
        day_offset, daypart = mapping
        target_date = (dt_util.now() + timedelta(days=day_offset)).date()
        self.hass.async_create_task(self._arm(target_date, daypart))

    async def _arm(self, target_date, daypart: str) -> None:
        date_id = self._entity_id("date", "return_date")
        daypart_id = self._entity_id("select", "return_daypart")
        if date_id:
            await self.hass.services.async_call(
                "date", "set_value",
                {ATTR_ENTITY_ID: date_id, "date": target_date.isoformat()},
                blocking=True,
            )
        if daypart_id:
            await self.hass.services.async_call(
                "select", "select_option",
                {ATTR_ENTITY_ID: daypart_id, "option": daypart},
                blocking=True,
            )
        await self._set_armed(True)

    async def _set_armed(self, on: bool) -> None:
        armed_id = self._entity_id("switch", "return_armed")
        if armed_id:
            await self.hass.services.async_call(
                "switch", "turn_on" if on else "turn_off",
                {ATTR_ENTITY_ID: armed_id}, blocking=True,
            )
