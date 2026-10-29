"""Palestra + bagno palestra damp alerts (v0.90.0, STORY_GYM_DAMP.md).

Read-only: ticks off the coordinator, reads the GymDampController's `view` and
pages the window-alert recipients once per episode:

1. `rh_ctrl ≥ 75 %` for 6 h while we drive the zone ("non tengo");
2. the dehumidifier commanded ON but drawing < `GYM_DEHUM_RUN_W` for 30 min
   (its own humidistat satisfied, a fault, or no restart after a power cut);
3. every hygrometer out for 2 h (the controller has just stopped the plug).

`switch.gym_damp_alerts` (default ON, independent of the master).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    GYM_ALERT_BLIND_FOR,
    GYM_ALERT_DAMP_FOR,
    GYM_ALERT_DAMP_RH,
    GYM_ALERT_IDLE_FOR,
    GYM_ALERT_TAG,
    GYM_DEHUM_RUN_W,
    OPT_WINDOW_ALERT_TARGETS,
    WINDOW_ALERT_TARGETS,
)
from .controller import _switch_state

_LOGGER = logging.getLogger(__name__)


def gym_alerts_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return _switch_state(hass, entry, "gym_damp_alerts") == STATE_ON


@dataclass
class _Episode:
    since: datetime | None = None
    sent: bool = False


class GymDampAlerts:
    """Once-per-episode pages for a palestra we can't keep dry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub = None
        self._episodes: dict[str, _Episode] = {}
        self._busy = False

    def start(self) -> None:
        self._unsub = self.entry.runtime_data.async_add_listener(self._on_update)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_update(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.hass.async_create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.async_check(dt_util.utcnow())
        finally:
            self._busy = False

    async def async_check(self, now: datetime) -> None:
        if not gym_alerts_enabled(self.hass, self.entry):
            self._episodes.clear()
            return
        gym = getattr(self.entry.runtime_data, "gym", None)
        v = getattr(gym, "view", None)
        if not v:
            return
        # only while we drive the zone: otherwise nobody promised anything
        enabled = bool(v.get("enabled"))
        rh = v.get("rh_ctrl")
        await self._episode(
            "damp", enabled and rh is not None and rh >= GYM_ALERT_DAMP_RH, now,
            GYM_ALERT_DAMP_FOR, "Palestra troppo umida",
            f"Umidità {rh:.0f}% da oltre 6 ore: non riesco a tenerla sotto "
            f"{v.get('ceiling'):.0f}%. Controlla il deumidificatore del bagno e "
            "che la porta tra bagno e palestra sia aperta."
            if rh is not None else "",
        )
        power = v.get("dehum_power")
        await self._episode(
            "idle",
            enabled and bool(v.get("dehum_commanded")) and v.get("dehum_on") is True
            and power is not None and power < GYM_DEHUM_RUN_W,
            now, GYM_ALERT_IDLE_FOR, "Deumidificatore palestra fermo",
            f"La presa è accesa ma assorbe {power or 0:.0f} W da 30 minuti: "
            "controlla l'umidostato del deumidificatore (continuo/minimo), un "
            "guasto, o che sia ripartito dopo un'interruzione di corrente.",
        )
        await self._episode(
            "blind", enabled and v.get("source") == "nessuno", now,
            GYM_ALERT_BLIND_FOR, "Palestra: sensori di umidità assenti",
            "Da 2 ore nessun igrometro (KNX palestra, KNX bagno, UP Sense) "
            "risponde: ho spento il deumidificatore.",
        )

    async def _episode(
        self, key: str, condition: bool, now: datetime, after: timedelta,
        title: str, message: str,
    ) -> None:
        ep = self._episodes.setdefault(key, _Episode())
        if not condition:
            ep.since, ep.sent = None, False
            return
        if ep.since is None:
            ep.since = now
        if ep.sent or now - ep.since < after:
            return
        ep.sent = True
        _LOGGER.info("Gym damp alert %s: %s", key, title)
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
                        "title": title,
                        "message": message,
                        "data": {"tag": f"{GYM_ALERT_TAG}_{key}"},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - paging is best-effort, never fatal
                _LOGGER.warning("Gym damp alert: could not notify %s", target, exc_info=True)
