"""Wine-cellar alerts (v0.87.0, STORY_CELLAR.md).

Read-only: ticks off the coordinator, reads the SplitGroupController's
`cellar_view` (the fan-mixed reading + the season window) and pages the
window-alert recipients once per episode when the wine is out of its window
for long enough. `switch.cellar_alerts` (default ON, independent of the master).
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
    CELLAR_ALERT_BLIND_FOR,
    CELLAR_ALERT_COLD_FOR,
    CELLAR_ALERT_DAMP_FOR,
    CELLAR_ALERT_DAMP_RH,
    CELLAR_ALERT_DRY_FOR,
    CELLAR_ALERT_DRY_RH,
    CELLAR_ALERT_TAG,
    CELLAR_ALERT_WARM_FOR,
    OPT_WINDOW_ALERT_TARGETS,
    WINDOW_ALERT_TARGETS,
)
from .controller import _switch_state

_LOGGER = logging.getLogger(__name__)


def cellar_alerts_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return _switch_state(hass, entry, "cellar_alerts") == STATE_ON


@dataclass
class _Episode:
    since: datetime | None = None
    sent: bool = False


class CellarAlerts:
    """Once-per-episode pages for a wine cellar out of its window."""

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
        if not cellar_alerts_enabled(self.hass, self.entry):
            self._episodes.clear()
            return
        split = getattr(self.entry.runtime_data, "split", None)
        v = getattr(split, "cellar_view", None)
        if not v:
            return
        w = v.get("window") or {}
        t, rh = v.get("temp"), v.get("humidity")
        await self._episode(
            "warm", t is not None and t > w.get("t_high", 99), now,
            CELLAR_ALERT_WARM_FOR, "Cantina vini calda",
            f"{t:.1f} °C da oltre 3 ore (finestra fino a {w.get('t_high')} °C). "
            "Controlla lo split della cantina e che il garage non sia in caldo."
            if t is not None else "",
        )
        await self._episode(
            "cold", t is not None and t < w.get("t_low", -99), now,
            CELLAR_ALERT_COLD_FOR, "Cantina vini fredda",
            f"{t:.1f} °C da oltre 6 ore (sotto {w.get('t_low')} °C). "
            "Nessun riscaldamento è automatico in cantina."
            if t is not None else "",
        )
        await self._episode(
            "damp", rh is not None and rh > CELLAR_ALERT_DAMP_RH, now,
            CELLAR_ALERT_DAMP_FOR, "Cantina vini umida",
            f"Umidità {rh:.0f}% da oltre 6 ore: rischio muffa su etichette e "
            "scaffali." if rh is not None else "",
        )
        await self._episode(
            "dry", rh is not None and rh < CELLAR_ALERT_DRY_RH, now,
            CELLAR_ALERT_DRY_FOR, "Cantina vini secca",
            f"Umidità {rh:.0f}% da oltre un giorno: i tappi possono seccarsi. "
            "Lo split può solo deumidificare: valuta un umidificatore."
            if rh is not None else "",
        )
        # blind only while we drive the fan: otherwise nobody promised a reading
        await self._episode(
            "blind", bool(v.get("enabled")) and t is None, now,
            CELLAR_ALERT_BLIND_FOR, "Cantina vini: nessuna misura",
            "Da 3 ore manca una lettura con il ventilatore acceso: controlla "
            "il ventilatore e il sensore della cantina.",
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
        _LOGGER.info("Cellar alert %s: %s", key, title)
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
                        "data": {"tag": f"{CELLAR_ALERT_TAG}_{key}"},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - paging is best-effort, never fatal
                _LOGGER.warning("Cellar alert: could not notify %s", target, exc_info=True)
