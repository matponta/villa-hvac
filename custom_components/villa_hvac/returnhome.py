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
from datetime import datetime, timedelta

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
    DEFAULT_WINTER_RETURN_MAX_HOLD,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_VACATION,
    OPT_NOTIFY_TARGET,
    OPT_RETURN_MARGIN_MIN,
    OPT_RETURN_MAX_LEAD_HOURS,
    OPT_WINTER_RETURN_MAX_HOLD,
    RETURN_ACTION_PREFIX,
    RETURN_ACTION_UNKNOWN,
    RETURN_DAYPART_EVENING,
    RETURN_DAYPART_MORNING,
    RETURN_NOTIFY_TAG,
    SEASON_WINTER,
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
from .supervisor.winter_plan import DepthHold, hold_depth

_LOGGER = logging.getLogger(__name__)


def _opt_float(entry: ConfigEntry | None, key: str, default: float) -> float:
    if entry is None:
        return default
    try:
        return float(entry.options.get(key, default))
    except (TypeError, ValueError):
        return default


class AwayReturnController:
    """Overrides the effective house mode while Via+armed (#8). Holds the latch."""

    def __init__(self, store=None) -> None:
        self._latched = False
        # v0.89.0: the ETA the latch was taken for (hold limit, re-plan on a
        # changed ETA) + a Store so a restart mid pre-heat keeps it (28/10:
        # a 12:54 restart dropped the house back to Via until someone arrived).
        self._latched_eta: datetime | None = None
        self._store = store
        self._saved: tuple | None = None
        # v0.85.0: the winter WAITING depth actually written (held, quantised).
        self._depth_hold = DepthHold()
        # Last computed view, for the diagnostic sensor (read-only).
        self.decision: str | None = None
        self.eta = None
        self.lead: timedelta | None = None
        self._max_hold: timedelta | None = None

    async def async_load(self) -> None:
        """v0.89.0: restore the latch persisted before a restart (best-effort)."""
        if self._store is None:
            return
        try:
            data = (await self._store.async_load()) or {}
            eta = dt_util.parse_datetime(data.get("eta") or "")
        except Exception:  # noqa: BLE001 - a corrupt file must never block setup
            _LOGGER.warning("Could not load the return latch", exc_info=True)
            return
        if data.get("latched") and eta is not None:
            self._latched, self._latched_eta = True, eta
            _LOGGER.info("Return pre-heat latch restored (ETA %s)", eta)
        self._saved = (self._latched, self._latched_eta)

    def _commit_latch(self, hass, latched: bool, eta) -> None:
        self._latched = latched
        self._latched_eta = eta if latched else None
        key = (self._latched, self._latched_eta)
        if self._store is None or key == self._saved:
            return
        self._saved = key
        data = {"latched": self._latched,
                "eta": self._latched_eta.isoformat() if self._latched_eta else None}

        async def _save() -> None:
            try:
                await self._store.async_save(data)
            except Exception:  # noqa: BLE001 - persistence is best-effort
                _LOGGER.debug("Return latch persist failed", exc_info=True)

        hass.async_create_task(_save())

    @property
    def hold_until(self) -> datetime | None:
        """v0.89.0: when an unanswered pre-heat gives up (None = not latched)."""
        if not self._latched or self._latched_eta is None or self._max_hold is None:
            return None
        return self._latched_eta + self._max_hold

    @property
    def written_depth(self) -> float | None:
        """v0.85.0: the winter setback depth W3 is writing now (None = none)."""
        return self._depth_hold.depth

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
        self, state, hass: HomeAssistant, entry: ConfigEntry, *, commit: bool,
        winter_advice=None,
    ):
        """Return `state`, possibly with house_mode/mode_offset overridden by #8.

        `commit` (= the engine is actuating) gates advancing the latch, mirroring
        how the duty/pacing timers only move on an actuating pass.
        """
        opt_in = return_precond_enabled(hass, entry)
        armed = return_armed(hass, entry)
        if state.season == SEASON_WINTER:
            self._max_hold = timedelta(hours=_opt_float(
                entry, OPT_WINTER_RETURN_MAX_HOLD, DEFAULT_WINTER_RETURN_MAX_HOLD))
            return self._apply_winter(
                state, opt_in, armed, winter_advice, commit, hass, self._max_hold)
        self._max_hold = None
        # Local wall clock (review: state.now is UTC, so a "sera 19:00" ETA
        # landed at 21:00 CEST and the summer pre-cond ramped 2 h late).
        eta = return_eta(
            return_date(hass, entry), return_daypart(hass, entry),
            DEFAULT_RETURN_DAYPART_HOURS, dt_util.as_local(state.now),
        )
        # Comfort target = the Casa setpoint (offset 0). Without it we can't size
        # the lead or the ramp -> stay inert.
        target = state.house_setpoint
        # Summer path (winter returned above via _apply_winter, v0.81.0).
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
            self._commit_latch(hass, new_latched, eta)
        self.decision, self.eta, self.lead = decision, eta, lead
        if decision == RETURN_WAITING:
            return replace(state, house_mode=HOUSE_MODE_VACATION, mode_offset=None)
        if decision == RETURN_PRECOND:
            return replace(state, house_mode=HOUSE_MODE_HOME, mode_offset=0.0)
        return state


    def _apply_winter(self, state, opt_in: bool, armed: bool, advice, commit: bool,
                      hass=None, max_hold: timedelta | None = None):
        """v0.81.0 W3 winter return pre-conditioning on the radiant model.

        WAITING keeps Via (comfort preset, setpoint-only winter) but with the
        ADVISED depth below Casa (W2) instead of the fixed Via offset; PRECOND
        switches to Casa at the advised start so the slow floor is warm by the
        ETA. Never building_protection (the v0.76 reason #8 was off in winter).
        No advice (no ETA / no data) → inert, native Via.

        v0.89.0: the start is the LIVE optimum start (W2 `start`); the latch
        remembers its ETA, is dropped when the armed ETA changes (re-plan), and
        gives up `max_hold` after the ETA when nobody came (24/10: Casa held
        21 h for an empty house) — back to native Via until re-armed."""
        is_via = state.house_mode == HOUSE_MODE_AWAY
        latched = self._latched
        if latched and is_via and armed and opt_in:
            new_eta = advice.eta if advice is not None else None
            if new_eta is not None and new_eta != self._latched_eta:
                # The owner moved the return: plan it afresh.
                latched = False
                if commit:
                    self._commit_latch(hass, False, None)
            elif (max_hold is not None and self._latched_eta is not None
                  and state.now >= self._latched_eta + max_hold):
                if commit:
                    _LOGGER.info("Return pre-heat: nobody home %s after the ETA; "
                                 "back to Via", max_hold)
                    self._commit_latch(hass, False, None)
                self.decision, self.eta, self.lead = None, self._latched_eta, None
                return state
            else:
                # Hold & wait for presence (review MAJOR): once pre-heating, stay
                # in Casa past the ETA and through any advice gap (no ETA left,
                # a sensor dropout) until the owner arrives (#2c → Casa), disarms
                # or the hold runs out.
                self.decision = RETURN_PRECOND
                return replace(state, house_mode=HOUSE_MODE_HOME, mode_offset=0.0)
        eta = advice.eta if advice is not None else None
        lead = (advice.eta - advice.start) if advice is not None else timedelta(0)
        decision, new_latched = return_decision(
            is_via=is_via and advice is not None, armed=armed, opt_in=opt_in,
            eta=eta, lead_time=lead, now=state.now, latched=latched,
        )
        if commit:
            self._commit_latch(hass, new_latched, eta)
        self.decision, self.eta, self.lead = decision, eta, lead
        if decision == RETURN_WAITING:
            # v0.85.0: write the predicted drop, quantised and held (it used to
            # write the raw depth every cycle: 24 setpoint changes on 23/10).
            hold = hold_depth(self._depth_hold, advice.setpoint_depth, state.now)
            if commit:
                self._depth_hold = hold
            return replace(state, mode_offset=-float(hold.depth))
        if commit:
            self._depth_hold = DepthHold()
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
