"""DataUpdateCoordinator for Villa HVAC.

Phase 0 (read-only): reads the real PdC call signals and per-zone cooling
demand (fancoil fan > 0). Control logic is layered on later platforms.
"""
from __future__ import annotations

from datetime import timedelta
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONSENSO_CALDO,
    CONSENSO_FREDDO,
    FANCOILS,
    TEMP_STALE_AFTER,
    ZONES,
)
from .temperature import TempSource, fuse_temperature

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
        # An OFF fan delivers 0% regardless of the % attribute the bus retains
        # (the switch GA and the speed GA are separate objects; proven live
        # 2026-07-02/03) — without this an off zone shows up in cooling_zones.
        if state.state == "off":
            return 0
        try:
            return int(float(state.attributes.get("percentage") or 0))
        except (TypeError, ValueError):
            return 0

    def _bin_state(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return state.state if state is not None else None

    def _age_s(self, state: State | None) -> float | None:
        if state is None:
            return None
        return (dt_util.utcnow() - state.last_updated).total_seconds()

    def _sensor_temp(self, entity_id: str | None) -> tuple[float | None, float | None]:
        """Temperature from a sensor whose state IS the value (e.g. clima_*)."""
        if not entity_id:
            return None, None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None, self._age_s(state)
        try:
            val = float(state.state)
        except (TypeError, ValueError):
            return None, self._age_s(state)
        return (val if math.isfinite(val) else None), self._age_s(state)

    def _climate_temp(self, entity_id: str | None) -> tuple[float | None, float | None]:
        """Temperature from a climate entity's `current_temperature` attribute."""
        if not entity_id:
            return None, None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, None
        raw = state.attributes.get("current_temperature")
        age = self._age_s(state)
        try:
            val = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None, age
        return (val if (val is not None and math.isfinite(val)) else None), age

    def _zone_temperature(self, zone: dict) -> dict[str, Any]:
        """Fuse a zone's temperature: clima_* primary, climate attr fallback."""
        primary_val, primary_age = self._sensor_temp(zone.get("temp_sensor"))
        fallback_climate = zone.get("temp_fallback_climate") or zone.get("climate")
        fallback_val, fallback_age = self._climate_temp(fallback_climate)
        value, source = fuse_temperature(
            [
                TempSource("sensor", primary_val, primary_age),
                TempSource("climate", fallback_val, fallback_age),
            ],
            TEMP_STALE_AFTER.total_seconds(),
        )
        return {
            "value": round(value, 1) if value is not None else None,
            "source": source,
            "sensor_raw": primary_val,
            "climate_raw": fallback_val,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        speeds = {eid: self._fan_pct(eid) for eid in FANCOILS}
        cooling_zones = [eid for eid, pct in speeds.items() if pct and pct > 0]
        zone_temps = {zid: self._zone_temperature(z) for zid, z in ZONES.items()}
        return {
            "speeds": speeds,
            "cooling_zones": cooling_zones,
            "cooling_zone_count": len(cooling_zones),
            "consenso_freddo": self._bin_state(CONSENSO_FREDDO),
            "consenso_caldo": self._bin_state(CONSENSO_CALDO),
            "zone_temps": zone_temps,
        }
