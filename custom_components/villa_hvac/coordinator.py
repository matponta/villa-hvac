"""DataUpdateCoordinator for Villa HVAC.

Phase 0 (read-only): reads the real PdC call signals and per-zone cooling
demand (fancoil fan > 0). Control logic is layered on later platforms.
"""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONSENSO_CALDO, CONSENSO_FREDDO, FANCOILS

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)


class VillaHvacCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls HA state for the HVAC call signals and fancoil demand."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Villa HVAC",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry

    def _fan_pct(self, entity_id: str) -> int | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return int(float(state.attributes.get("percentage") or 0))
        except (TypeError, ValueError):
            return 0

    def _bin_state(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return state.state if state is not None else None

    async def _async_update_data(self) -> dict[str, Any]:
        speeds = {eid: self._fan_pct(eid) for eid in FANCOILS}
        cooling_zones = [eid for eid, pct in speeds.items() if pct and pct > 0]
        return {
            "speeds": speeds,
            "cooling_zones": cooling_zones,
            "cooling_zone_count": len(cooling_zones),
            "consenso_freddo": self._bin_state(CONSENSO_FREDDO),
            "consenso_caldo": self._bin_state(CONSENSO_CALDO),
        }
