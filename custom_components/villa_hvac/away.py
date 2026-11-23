"""Away auto-escalation (#2c) + Via from the burglar alarm (v0.95.0).

Replaces the legacy automation.clima_backup_via_quando_esco: after the adults
are away for a configurable spell (while the house is in Casa or Notte), drop to
Via; when they return and the house is still in the *auto-set* Via, restore Casa.
Restore only triggers from Via, so it never overrides a manual Notte/Vacanza.

v0.95.0 (STORY_VIA_ANTIFURTO.md): `AlarmAwayController` drops to Via once per
alarm episode (mirror `Inserito`) after a per-season delay, with the adults'
trackers away. The 18 h tracker-only escalation stays as the safety net.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_HOME,
    STATE_NOT_HOME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ALARM_ARMED,
    ALARM_AWAY_NUMBERS,
    ALARM_AWAY_RECHECK,
    ALARM_AWAY_STORE,
    ALARM_MIRROR,
    DEFAULT_AWAY_HOURS,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_NIGHT,
    HOUSE_MODES,
    OPT_AWAY_HOURS,
    PRESENCE_PERSONS,
    SEASON_REFERENCE_CLIMATE,
    SEASON_STAGIONE_SENSOR,
    SEASON_WINTER,
)
from .controller import (
    alarm_away_delays,
    alarm_away_enabled,
    auto_setback_enabled,
    current_house_mode,
    current_season,
)

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
        await async_set_house_mode(self.hass, self.entry, mode)


async def async_set_house_mode(
    hass: HomeAssistant, entry: ConfigEntry, mode: str,
    context: Context | None = None,
) -> bool:
    """Select `mode` on our house-mode select (False if it isn't registered)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_house_mode"
    )
    if not entity_id:
        return False
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: entity_id, "option": mode},
        blocking=True,
        context=context,
    )
    return True


# --- Via from the burglar alarm (v0.95.0) -------------------------------------

# Why the alarm->Via is not firing (sensor.via_da_antifurto `reason`).
ALARM_REASON_READY = "pronto"
ALARM_REASON_SWITCH_OFF = "switch_spento"
ALARM_REASON_SETBACK_OFF = "auto_setback_spento"
ALARM_REASON_NOT_ARMED = "antifurto_non_inserito"
ALARM_REASON_PRESENCE_HOME = "presenza_in_casa"
ALARM_REASON_PRESENCE_UNKNOWN = "presenza_sconosciuta"
ALARM_REASON_MODE = "modalita_"            # + the current mode, lower-case
ALARM_REASON_WAITING = "in_attesa_ritardo"
ALARM_REASON_FIRED = "gia_scattato"
ALARM_REASON_CANCELLED = "modalita_cambiata_a_mano"

# sensor.via_da_antifurto states.
ALARM_STATUS_OFF = "spento"
ALARM_STATUS_WAITING = "in_attesa"
ALARM_STATUS_FIRED = "scattato"
ALARM_STATUS_CANCELLED = "annullato"
ALARM_STATUS_IDLE = "inattivo"

# The mirror reads one of these when HA itself doesn't know (boot, reload):
# no information — neither opens nor closes an episode.
_MIRROR_BLIND = (None, STATE_UNKNOWN, STATE_UNAVAILABLE)


def alarm_fire_at(
    armed_since: datetime | None, season: str, delays: dict[str, timedelta],
) -> datetime | None:
    """When the episode may drop the house to Via: armed_since + the delay of
    the CURRENT season (recomputed if the season or the knobs change)."""
    if armed_since is None:
        return None
    delay = delays.get(season, delays.get(SEASON_WINTER, timedelta(0)))
    return armed_since + delay


def alarm_away_due(
    mirror: str | None,
    armed_since: datetime | None,
    now: datetime,
    season: str,
    delays: dict[str, timedelta],
    presence: str | None,
    mode: str,
    enabled: bool,
    auto_setback: bool = True,
) -> tuple[bool, str]:
    """Pure: should the alarm episode drop the house to Via right now?

    All of: opt-in + Auto setback on, mirror `Inserito` (not `Inserito notte`),
    adults' fused presence not_home (None = can't tell -> no), mode Casa/Notte
    (Via/Vacanza untouched), and `now` past armed_since + the season delay.
    Returns (due, reason); the blocking conditions are checked before the
    clock so `reason` says what would stop it even while waiting.
    """
    if not enabled:
        return False, ALARM_REASON_SWITCH_OFF
    if not auto_setback:
        return False, ALARM_REASON_SETBACK_OFF
    if mirror != ALARM_ARMED or armed_since is None:
        return False, ALARM_REASON_NOT_ARMED
    if presence == STATE_HOME:
        return False, ALARM_REASON_PRESENCE_HOME
    if presence != STATE_NOT_HOME:
        return False, ALARM_REASON_PRESENCE_UNKNOWN
    if escalation_target(mode) is None:
        return False, f"{ALARM_REASON_MODE}{str(mode).lower()}"
    fire_at = alarm_fire_at(armed_since, season, delays)
    if fire_at is None or now < fire_at:
        return False, ALARM_REASON_WAITING
    return True, ALARM_REASON_READY


def alarm_signal(entry: ConfigEntry) -> str:
    """Dispatcher signal: the alarm->Via view changed (the sensor refreshes)."""
    return f"{DOMAIN}_{entry.entry_id}_alarm_away"


class AlarmAwayController:
    """v0.95.0: one Via per alarm episode, after the season's delay.

    Episode = mirror `Inserito`; it ends on any other mirror value (Allarme and
    Sconosciuto included) without firing. `armed_since` is persisted so a
    restart neither restarts nor forgets the count. The trigger is a
    point-in-time timer on `fire_at`, re-planned on every input change and
    every few minutes, and every check re-reads all the conditions.
    A house-mode change made by anyone else before the fire cancels the
    episode (the manual choice wins); after the fire nothing is re-asserted
    (Casa set by hand to pre-heat for the return stays Casa).
    Return to Casa stays with `AwayController` (presence home) — never on
    `Disinserito` (Ajax disarms on a schedule for the cleaner).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store=None) -> None:
        self.hass = hass
        self.entry = entry
        self._store = store if store is not None else Store(hass, 1, ALARM_AWAY_STORE)
        self.armed_since: datetime | None = None
        self.fired = False
        self.cancelled = False
        self._own_contexts: deque[str] = deque(maxlen=8)
        self._unsubs: list = []
        self._cancel_timer = None
        self._timer_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._stopped = False
        self.view: dict = {"status": ALARM_STATUS_IDLE}

    # -- lifecycle -------------------------------------------------------------

    async def async_start(self) -> None:
        data: dict = {}
        try:
            data = (await self._store.async_load()) or {}
        except Exception:  # noqa: BLE001 - a corrupt file must never block setup
            _LOGGER.warning("Could not load the alarm->Via episode", exc_info=True)
        saved = dt_util.parse_datetime(data.get("armed_since") or "")
        mirror = self._mirror()
        if mirror == ALARM_ARMED:
            if saved is not None:
                self.armed_since = saved
                self.fired = bool(data.get("fired"))
                self.cancelled = bool(data.get("cancelled"))
                _LOGGER.info("Alarm->Via episode resumed (armed since %s)", saved)
            else:
                # Nothing saved: count from now (conservative, never early).
                self.armed_since = dt_util.utcnow()
                await self._save()
        elif mirror in _MIRROR_BLIND:
            # The mirror hasn't restored yet: keep what was saved; its
            # Inserito/other value arriving later resumes or closes it.
            if saved is not None:
                self.armed_since = saved
                self.fired = bool(data.get("fired"))
                self.cancelled = bool(data.get("cancelled"))
        elif saved is not None:
            await self._save()  # mirror says the episode is over: forget it

        hass = self.hass
        self._unsubs.append(async_track_state_change_event(
            hass, [ALARM_MIRROR], self._on_mirror))
        if (mode_id := self._entity_id("select", "house_mode")) is not None:
            self._unsubs.append(async_track_state_change_event(
                hass, [mode_id], self._on_mode))
        inputs = list(PRESENCE_PERSONS) + [
            SEASON_REFERENCE_CLIMATE, SEASON_STAGIONE_SENSOR]
        for domain, suffix in [("switch", "alarm_away"), ("switch", "auto_setback")] + [
            ("number", s) for s in ALARM_AWAY_NUMBERS
        ]:
            if (eid := self._entity_id(domain, suffix)) is not None:
                inputs.append(eid)
        self._unsubs.append(async_track_state_change_event(
            hass, inputs, self._on_input))
        self._unsubs.append(async_track_time_interval(
            hass, self._on_tick, ALARM_AWAY_RECHECK))
        await self._async_evaluate()

    def stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._clear_timer()

    # -- inputs ----------------------------------------------------------------

    def _entity_id(self, domain: str, suffix: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            domain, DOMAIN, f"{self.entry.entry_id}_{suffix}")

    def _mirror(self) -> str | None:
        st = self.hass.states.get(ALARM_MIRROR)
        return st.state if st is not None else None

    def _presence(self) -> str | None:
        states = []
        for person in PRESENCE_PERSONS:
            st = self.hass.states.get(person)
            states.append(st.state if st is not None else None)
        return aggregate_presence(states)

    def _kick(self) -> None:
        if not self._stopped:
            self.hass.async_create_task(self._async_evaluate())

    @callback
    def _on_mirror(self, event: Event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        old_s = old.state if old is not None else None
        new_s = new.state if new is not None else None
        if new_s in _MIRROR_BLIND:
            self._kick()  # no information: neither opens nor closes
            return
        if new_s == ALARM_ARMED:
            if old_s in _MIRROR_BLIND and self.armed_since is not None:
                pass  # mirror restored after a reload: same episode
            elif old_s != ALARM_ARMED or self.armed_since is None:
                self.armed_since = new.last_changed if new is not None else dt_util.utcnow()
                self.fired = self.cancelled = False
                _LOGGER.info("Alarm armed: alarm->Via episode opened")
        elif self.armed_since is not None:
            _LOGGER.info("Alarm %s: alarm->Via episode closed", new_s)
            self.armed_since = None
            self.fired = self.cancelled = False
        self.hass.async_create_task(self._save())
        self._kick()

    @callback
    def _on_mode(self, event: Event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if old is None or new is None or old.state == new.state:
            return
        if old.state not in HOUSE_MODES or new.state not in HOUSE_MODES:
            return  # restore / unavailable churn, not a choice
        if new.context is not None and new.context.id in self._own_contexts:
            return  # our own fire
        if self.armed_since is not None and not self.fired and not self.cancelled:
            _LOGGER.info("House mode changed to %s by hand: alarm->Via episode "
                         "cancelled", new.state)
            self.cancelled = True
            self.hass.async_create_task(self._save())
        self._kick()

    @callback
    def _on_input(self, _event: Event) -> None:
        self._kick()

    @callback
    def _on_tick(self, _now=None) -> None:
        self._kick()

    @callback
    def _on_timer(self, _now) -> None:
        self._cancel_timer = None
        self._timer_at = None
        self._kick()

    def _clear_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = None
        self._timer_at = None

    def _plan_timer(self, when: datetime | None) -> None:
        if when == self._timer_at:
            return
        self._clear_timer()
        if when is not None and not self._stopped:
            self._timer_at = when
            self._cancel_timer = async_track_point_in_time(
                self.hass, self._on_timer, when)

    async def _save(self) -> None:
        data = {
            "armed_since": self.armed_since.isoformat() if self.armed_since else None,
            "fired": self.fired,
            "cancelled": self.cancelled,
        }
        try:
            await self._store.async_save(data)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            _LOGGER.debug("Alarm->Via persist failed", exc_info=True)

    # -- decision --------------------------------------------------------------

    async def _async_evaluate(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            now = dt_util.utcnow()
            season = current_season(self.hass, self.entry)
            delays = alarm_away_delays(self.hass, self.entry)
            enabled = alarm_away_enabled(self.hass, self.entry)
            mirror = self._mirror()
            presence = self._presence()
            mode = current_house_mode(self.hass, self.entry)
            fire_at = alarm_fire_at(self.armed_since, season, delays)
            if self.fired:
                due, reason = False, ALARM_REASON_FIRED
            elif self.cancelled:
                due, reason = False, ALARM_REASON_CANCELLED
            else:
                due, reason = alarm_away_due(
                    mirror, self.armed_since, now, season, delays, presence, mode,
                    enabled, auto_setback_enabled(self.hass, self.entry),
                )
            if due:
                context = Context()
                self._own_contexts.append(context.id)
                _LOGGER.info("Alarm armed since %s (%s, delay %s), adults away: "
                             "house -> Via", self.armed_since, season,
                             delays.get(season))
                if await async_set_house_mode(
                    self.hass, self.entry, HOUSE_MODE_AWAY, context
                ):
                    self.fired = True
                    reason = ALARM_REASON_FIRED
                    await self._save()
            waiting = (self.armed_since is not None and not self.fired
                       and not self.cancelled)
            self._plan_timer(fire_at if waiting and fire_at and fire_at > now else None)
            if not enabled:
                status = ALARM_STATUS_OFF
            elif self.armed_since is None:
                status = ALARM_STATUS_IDLE
            elif self.fired:
                status = ALARM_STATUS_FIRED
            elif self.cancelled:
                status = ALARM_STATUS_CANCELLED
            else:
                status = ALARM_STATUS_WAITING
            delay = delays.get(season)
            self.view = {
                "status": status,
                "armed_since": self.armed_since,
                "fire_at": fire_at,
                "season": season,
                "delay": str(delay) if delay is not None else None,
                "delay_minutes": (round(delay.total_seconds() / 60)
                                  if delay is not None else None),
                "reason": reason,
                "mirror": mirror,
                "presence": presence,
                "house_mode": mode,
            }
        async_dispatcher_send(self.hass, alarm_signal(self.entry))
