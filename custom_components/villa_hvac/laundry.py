"""Laundry drying through the lavanderia vasistas (v0.97.0, STORY_LAUNDRY.md).

HA wrapper around the pure `supervisor/laundry.py` law: reads the cover, the
lavanderia KNX thermostat (temperature + RH) and the gw3000a outdoor station,
writes `cover.open_cover` / `cover.close_cover`, persists its bookkeeping (so a
restart mid-purge still closes on time) and exposes a `view` for
`sensor.lavanderia_asciugatura`.

EDGE-TRIGGERED and OUTSIDE the reconcile arbiter, like the VMC boost and the
winter sun: it writes only on its own decision, never re-asserts, and an opening
by hand is left alone until its cap.

Two hooks into the rest of the integration:
- `owns_purge(zone)` — the #4 window pause does NOT put the lavanderia in
  building_protection for one of OUR short purges (the radiant keeps heating:
  warm air is what dries the laundry). A manual opening still pauses as before.
- `async_release()` — HA stop / entry unload close a purge of ours: a vasistas
  must never stay open because the supervisor died.

Default ON (`switch.laundry_drying`) on top of the master switch.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    LAUNDRY_ALERT_TAG,
    LAUNDRY_CLOSE_RETRIES,
    LAUNDRY_RAIN_RATE,
    LAUNDRY_RH,
    LAUNDRY_SENSOR_STALE,
    LAUNDRY_TEMP,
    LAUNDRY_VASISTAS,
    LAUNDRY_ZONE,
    OPT_WINDOW_ALERT_TARGETS,
    OUTDOOR_RH,
    OUTDOOR_TEMP,
    RAIN_RATE,
    WINDOW_ALERT_TARGETS,
    ZONES,
)
from .controller import laundry_drying_enabled
from .engine import LEVER_CALL_TIMEOUT
from .supervisor.laundry import (
    CLOSE_MANUAL_MAX,
    CMD_CLOSE,
    CMD_OPEN,
    LaundryDecision,
    LaundryParams,
    LaundryReading,
    LaundryState,
    failed_command,
    laundry_step,
)

_LOGGER = logging.getLogger(__name__)

_OPEN = ("open",)
_CLOSED = ("closed",)
_MOVING = ("opening", "closing")
PARAMS = LaundryParams()


def _float(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class LaundryController:
    """Short drying purges through the lavanderia vasistas (default ON)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.state = LaundryState()
        self.view: dict = {}
        self._store = Store(hass, 1, "villa_hvac_laundry")
        self._lock = asyncio.Lock()
        self._unsub = None
        self._stopped = False
        self._close_failures = 0

    # -- lifecycle -------------------------------------------------------------
    async def async_start(self) -> None:
        try:
            self.state = _decode(await self._store.async_load())
        except Exception:  # noqa: BLE001 - a corrupt store must never block setup
            _LOGGER.warning("Laundry: could not load its store", exc_info=True)
            self.state = LaundryState()
        self._unsub = self.entry.runtime_data.async_add_listener(self._on_update)

    async def async_stop(self) -> None:
        """Entry unload: close a purge of ours, then stop listening."""
        await self.async_release()
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def async_release(self) -> None:
        """Close the vasistas if it is open for OUR purge (HA stop / unload).
        A manual opening is never touched here."""
        async with self._lock:
            if self._stopped or not self.state.ours:
                return
            st = self.hass.states.get(LAUNDRY_VASISTAS)
            if st is None or st.state in _CLOSED:
                return
            ok = await self._write(CMD_CLOSE)
            _LOGGER.info("Laundry: release closes our purge%s", "" if ok else " (FAILED)")
            if ok:
                self.state = LaundryState(
                    closed_since=dt_util.utcnow(),
                    purges_day=self.state.purges_day, purges=self.state.purges,
                )
                await self._save()

    @callback
    def _on_update(self) -> None:
        # Tied to the entry: cancelled on unload/reload.
        self.entry.async_create_background_task(
            self.hass, self._evaluate(), "villa_hvac laundry"
        )

    async def async_evaluate(self) -> None:
        """Decide now (the switch calls this so OFF acts without a tick)."""
        await self._evaluate()

    # -- hook for the #4 window pause -----------------------------------------
    def owns_purge(self, zone_id: str) -> bool:
        """True while the zone's opening is OUR purge, within its cap (+ the
        grace): the window pause skips it. A purge that overstays (a close that
        won't land) loses the exemption, so the room is paused as a normal
        open window."""
        if zone_id != LAUNDRY_ZONE or not self.state.ours:
            return False
        since = self.state.open_since
        if since is None:
            return False
        cap = PARAMS.max_open_mild + PARAMS.grace * 2
        return dt_util.utcnow() - since <= cap

    # -- one cycle -------------------------------------------------------------
    def _fresh(self, entity_id: str, now: datetime) -> float | None:
        st = self.hass.states.get(entity_id)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        seen = getattr(st, "last_reported", None) or st.last_updated
        if seen is not None and now - seen > LAUNDRY_SENSOR_STALE:
            return None
        return _float(st.state)

    def _reading(self, now: datetime) -> LaundryReading:
        cover = self.hass.states.get(LAUNDRY_VASISTAS)
        state = cover.state if cover is not None else None
        is_open = True if state in _OPEN + _MOVING else False if state in _CLOSED else None
        room_temp = self._fresh(LAUNDRY_TEMP, now)
        room_rh = self._fresh(LAUNDRY_RH, now)
        if room_temp is None or room_rh is None:
            climate = self.hass.states.get(ZONES[LAUNDRY_ZONE]["climate"])
            attrs = climate.attributes if climate is not None else {}
            if room_temp is None:
                room_temp = _float(attrs.get("current_temperature"))
            if room_rh is None:
                room_rh = _float(attrs.get("current_humidity"))
        rain = self._fresh(RAIN_RATE, now)
        return LaundryReading(
            is_open=is_open,
            moving=state in _MOVING,
            room_temp=room_temp,
            room_rh=room_rh,
            outdoor_temp=self._fresh(OUTDOOR_TEMP, now),
            outdoor_rh=self._fresh(OUTDOOR_RH, now),
            # Missing gauge = not raining (the AH gate still guards fog/drizzle).
            raining=rain is not None and rain >= LAUNDRY_RAIN_RATE,
        )

    async def _evaluate(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            engine = getattr(self.entry.runtime_data, "engine", None)
            if engine is None:
                return
            now = dt_util.utcnow()
            enabled = engine.enabled and laundry_drying_enabled(self.hass, self.entry)
            reading = self._reading(now)
            before = self.state
            decision = laundry_step(
                enabled=enabled, reading=reading, state=before, now=now,
                local_day=dt_util.as_local(now).date().isoformat(), params=PARAMS,
            )
            self.state = decision.state
            if decision.command is not None and not self._stopped:
                ok = await self._write(decision.command)
                _LOGGER.info(
                    "Laundry: %s vasistas (%s)%s", decision.command,
                    decision.close_reason or decision.reason, "" if ok else " (FAILED)",
                )
                if not ok:
                    self.state = failed_command(before, self.state, decision.command)
                    if decision.command == CMD_CLOSE:
                        self._close_failures += 1
                        if self._close_failures == LAUNDRY_CLOSE_RETRIES:
                            _LOGGER.warning(
                                "Laundry: the vasistas has refused %d closes in a row",
                                self._close_failures,
                            )
                else:
                    self._close_failures = 0
                    if decision.close_reason == CLOSE_MANUAL_MAX:
                        await self._notify_manual_close(reading, before)
            self._set_view(decision, reading, enabled)
            if self.state != before and not self._stopped:
                await self._save()

    def _set_view(self, d: LaundryDecision, r: LaundryReading, enabled: bool) -> None:
        def _iso(when: datetime | None) -> str | None:
            return dt_util.as_local(when).isoformat() if when else None

        def _r(v: float | None) -> float | None:
            return round(v, 2) if v is not None else None

        self.view = {
            "reason": d.reason,
            "enabled": enabled,
            "vasistas_open": r.is_open,
            "ours": d.state.ours,
            "open_since": _iso(d.state.open_since),
            "closed_since": _iso(d.state.closed_since),
            "max_open_min": int(d.max_open.total_seconds() // 60) if d.max_open else None,
            "next_purge_at": _iso(d.next_purge_at),
            "last_close_reason": d.close_reason or self.view.get("last_close_reason"),
            "purges_today": d.state.purges,
            "room_temp": r.room_temp,
            "room_rh": r.room_rh,
            "outdoor_temp": r.outdoor_temp,
            "outdoor_rh": r.outdoor_rh,
            "ah_room": _r(d.ah_room),
            "ah_outdoor": _r(d.ah_outdoor),
            "cold": d.cold,
            "raining": r.raining,
            "rh_start": PARAMS.rh_start,
            "rh_stop": PARAMS.rh_stop,
        }

    async def _write(self, command: str) -> bool:
        service = "open_cover" if command == CMD_OPEN else "close_cover"
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "cover", service, {"entity_id": LAUNDRY_VASISTAS}, blocking=True,
                ),
                LEVER_CALL_TIMEOUT,
            )
            return True
        except Exception:  # noqa: BLE001 - one wedged cover must not break the tick
            _LOGGER.warning("Laundry: could not %s the vasistas", command, exc_info=True)
            return False

    async def _notify_manual_close(self, r: LaundryReading, before: LaundryState) -> None:
        minutes = (
            int((dt_util.utcnow() - before.open_since).total_seconds() // 60)
            if before.open_since else None
        )
        outside = f" con {r.outdoor_temp:.0f} °C fuori" if r.outdoor_temp is not None else ""
        message = (
            f"Il vasistas della lavanderia era aperto da {minutes} minuti{outside}: "
            "l'ho chiuso. Se c'è bucato steso, ci penso io con ricambi d'aria brevi."
        )
        raw = self.entry.options.get(OPT_WINDOW_ALERT_TARGETS)
        targets = (
            [t.strip() for t in str(raw).split(",") if t.strip()] if raw
            else list(WINDOW_ALERT_TARGETS)
        )
        for target in targets:
            try:
                await self.hass.services.async_call(
                    "notify", target.removeprefix("notify."),
                    {
                        "title": "Vasistas lavanderia chiuso",
                        "message": message,
                        "data": {"tag": LAUNDRY_ALERT_TAG},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - paging is best-effort, never fatal
                _LOGGER.warning("Laundry: could not notify %s", target, exc_info=True)

    async def _save(self) -> None:
        try:
            await self._store.async_save(_encode(self.state))
        except Exception:  # noqa: BLE001 - best-effort bookkeeping
            _LOGGER.warning("Laundry: could not save its store", exc_info=True)


def _encode(state: LaundryState) -> dict:
    def _iso(when: datetime | None) -> str | None:
        return when.isoformat() if when else None

    return {
        "ours": state.ours,
        "open_since": _iso(state.open_since),
        "closed_since": _iso(state.closed_since),
        "commanded_at": _iso(state.commanded_at),
        "commanded": state.commanded,
        "purges_day": state.purges_day,
        "purges": state.purges,
    }


def _decode(data) -> LaundryState:
    if not isinstance(data, dict):
        return LaundryState()

    def _when(key: str) -> datetime | None:
        raw = data.get(key)
        return dt_util.parse_datetime(raw) if isinstance(raw, str) else None

    commanded = data.get("commanded")
    try:
        purges = max(0, int(data.get("purges") or 0))
    except (TypeError, ValueError):
        purges = 0
    return LaundryState(
        ours=bool(data.get("ours")),
        open_since=_when("open_since"),
        closed_since=_when("closed_since"),
        commanded_at=_when("commanded_at"),
        commanded=commanded if commanded in (CMD_OPEN, CMD_CLOSE) else None,
        purges_day=data.get("purges_day") if isinstance(data.get("purges_day"), str) else None,
        purges=purges,
    )
