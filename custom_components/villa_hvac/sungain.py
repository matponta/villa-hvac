"""Winter solar gain (v0.78.0): open the sunny covers while the house is empty.

In winter, with the house in Via or Vacanza and real sun on a facade, the
covers on that facade are opened (once per day each) to let the free heat in;
at sunset the covers WE opened go back to where they were. Pure decision in
`supervisor/sungain.py`; this wrapper reads HA state, writes the covers and
persists the small bookkeeping (so a restart mid-afternoon still closes them).

EDGE-TRIGGERED and OUTSIDE the reconcile arbiter, like the VMC boost: it writes
only on its own decision, never re-asserts, and a cover moved by hand is left to
the owner (the v0.41 Via full-close fought the owner through the 2 h override
backoff — never again). Default ON (`switch.winter_sun`) on top of the master.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_WINTER_SUN_SOLAR,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_VACATION,
    OPT_WINTER_SUN_SOLAR,
    SEASON_WINTER,
    SHADE_POSITION_TOLERANCE,
    SHADING_AZIMUTH_BANDS,
    SHADING_MIN_ELEVATION,
    SOLAR_RADIATION,
    WINTER_SUN_CLOSE_RETRIES,
    WINTER_SUN_TAKEOVER_GRACE_MIN,
)
from .controller import (
    _entity_id,
    season_conclusive,
    current_house_mode,
    current_season,
    shade_blocked,
    winter_sun_enabled,
)
from .engine import LEVER_CALL_TIMEOUT
from .policies import _azimuth_in_band
from .supervisor.sungain import SunCover, WinterSunState, winter_sun_step

_LOGGER = logging.getLogger(__name__)

_MOVING = ("opening", "closing")


def _float(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class WinterSunController:
    """Edge-triggered winter solar-gain cover opener (default ON)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.state = WinterSunState()
        self._store = Store(hass, 1, "villa_hvac_winter_sun")
        self._lock = asyncio.Lock()
        self._unsub = None
        self._stopped = False
        # cover -> consecutive failed sunset put-backs (retried, then given up).
        self._close_failures: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------------
    async def async_start(self) -> None:
        try:
            self.state = _decode(await self._store.async_load())
        except Exception:  # noqa: BLE001 - a corrupt store must never block setup
            _LOGGER.warning("Winter sun: could not load its store", exc_info=True)
            self.state = WinterSunState()
        self._unsub = self.entry.runtime_data.async_add_listener(self._on_update)

    async def async_stop(self) -> None:
        """Unsubscribe. Never moves a cover on shutdown — the persisted state
        lets the next boot finish the day (sunset close) instead."""
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_update(self) -> None:
        # Tied to the entry: cancelled on unload/reload, so a queued cycle of an
        # old instance can never write covers or clobber the shared Store.
        self.entry.async_create_background_task(
            self.hass, self._evaluate(), "villa_hvac winter sun"
        )

    # -- one cycle -------------------------------------------------------------
    def _covers(self, engine) -> tuple[SunCover, ...]:
        out = []
        for c in engine._resolve_covers():
            st = self.hass.states.get(c.entity_id)
            position = None
            moving = False
            if st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                pos = _float(st.attributes.get("current_position"))
                position = int(pos) if pos is not None else None
                moving = st.state in _MOVING
            out.append(SunCover(
                entity_id=c.entity_id,
                orientation=c.orientation,
                blocked=shade_blocked(self.hass, self.entry, c.zone) if c.zone else False,
                position=position,
                moving=moving,
            ))
        return tuple(out)

    def _ready(self) -> bool:
        """The mode select + master switch have real states. At boot they can
        be missing for a tick; reading that as "home / master off" would FORGET
        the covers opened today and leave them open overnight."""
        for domain, suffix in (("select", "house_mode"), ("switch", "supervisor")):
            eid = _entity_id(self.hass, self.entry, domain, suffix)
            st = self.hass.states.get(eid) if eid else None
            if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False
        # ...and a LIVE season signal: a memory/calendar guess must not make us
        # forget (season "not winter") or move covers (review v0.78.0).
        return season_conclusive(self.hass, self.entry)

    def _threshold(self) -> float:
        v = _float(self.entry.options.get(OPT_WINTER_SUN_SOLAR, DEFAULT_WINTER_SUN_SOLAR))
        return v if v is not None else DEFAULT_WINTER_SUN_SOLAR

    async def _evaluate(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            engine = getattr(self.entry.runtime_data, "engine", None)
            if engine is None or not self._ready():
                return  # boot: hold the persisted bookkeeping, decide nothing
            now = dt_util.utcnow()
            active = (
                engine.enabled
                and winter_sun_enabled(self.hass, self.entry)
                and current_season(self.hass, self.entry) == SEASON_WINTER
            )
            away = current_house_mode(self.hass, self.entry) in (
                HOUSE_MODE_AWAY, HOUSE_MODE_VACATION,
            )
            sun = self.hass.states.get("sun.sun")
            elevation = _float(sun.attributes.get("elevation")) if sun else None
            azimuth = _float(sun.attributes.get("azimuth")) if sun else None
            sun_on = (
                frozenset(o for o in SHADING_AZIMUTH_BANDS if _azimuth_in_band(azimuth, o))
                if azimuth is not None else frozenset()
            )
            solar_state = self.hass.states.get(SOLAR_RADIATION)
            solar = _float(solar_state.state) if solar_state is not None else None
            before = self.state
            self.state, commands = winter_sun_step(
                active=active,
                away=away,
                sun_elevation=elevation,
                sun_on=sun_on,
                bright=solar is not None and solar >= self._threshold(),
                covers=self._covers(engine) if active else (),
                now=now,
                local_day=dt_util.as_local(now).date().isoformat(),
                state=before,
                min_elevation=SHADING_MIN_ELEVATION,
                tolerance=SHADE_POSITION_TOLERANCE,
                grace=timedelta(minutes=WINTER_SUN_TAKEOVER_GRACE_MIN),
            )
            sunset = elevation is not None and elevation <= 0
            for entity_id, position in commands.items():
                if self._stopped:
                    return
                ok = await self._write(entity_id, position)
                _LOGGER.info(
                    "Winter sun: %s -> %s%s", entity_id, position, "" if ok else " (FAILED)"
                )
                if ok:
                    self._close_failures.pop(entity_id, None)
                elif not sunset:
                    # A failed open must not be "closed back" at sunset.
                    self.state.opened.pop(entity_id, None)
                else:
                    # A failed put-back is retried next tick (KNX hiccup), a
                    # bounded number of times — never a cover up all night
                    # because of one lost telegram, never an endless retry.
                    n = self._close_failures.get(entity_id, 0) + 1
                    self._close_failures[entity_id] = n
                    if n < WINTER_SUN_CLOSE_RETRIES and entity_id in before.opened:
                        self.state.opened[entity_id] = before.opened[entity_id]
                    else:
                        _LOGGER.warning(
                            "Winter sun: giving up putting back %s after %d tries",
                            entity_id, n,
                        )
                        self._close_failures.pop(entity_id, None)
            if self.state != before and not self._stopped:
                await self._save()

    async def _write(self, entity_id: str, position: int) -> bool:
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "cover", "set_cover_position",
                    {"entity_id": entity_id, "position": position}, blocking=True,
                ),
                LEVER_CALL_TIMEOUT,
            )
            return True
        except Exception:  # noqa: BLE001 - one wedged cover must not break the tick
            _LOGGER.warning("Winter sun: could not move %s", entity_id, exc_info=True)
            return False

    async def _save(self) -> None:
        try:
            await self._store.async_save(_encode(self.state))
        except Exception:  # noqa: BLE001 - best-effort bookkeeping
            _LOGGER.warning("Winter sun: could not save its store", exc_info=True)


def _encode(state: WinterSunState) -> dict:
    return {
        "day": state.day,
        "done": sorted(state.done),
        "opened": {
            eid: [prior, at.isoformat()] for eid, (prior, at) in state.opened.items()
        },
    }


def _decode(data) -> WinterSunState:
    if not isinstance(data, dict):
        return WinterSunState()
    opened: dict[str, tuple[int, datetime]] = {}
    for eid, value in (data.get("opened") or {}).items():
        try:
            prior, at = value
            when = dt_util.parse_datetime(at)
            if when is not None:
                opened[str(eid)] = (int(prior), when)
        except (TypeError, ValueError):
            continue
    return WinterSunState(
        day=data.get("day"),
        done=frozenset(str(e) for e in data.get("done") or ()),
        opened=opened,
    )
