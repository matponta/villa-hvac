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


def runtime_step(
    *,
    now_s: float,
    last_ts_s: float | None,
    last_consenso: str | None,
    consenso: str | None,
    runtime_s: float,
    cycles: int,
    poll_s: float,
) -> tuple[float, int]:
    """Pure #6 accumulation step: credit contiguous consenso-on time to run-time
    and count off->on transitions as compressor starts.

    A gap longer than 3x the poll interval is NOT credited (a restart gap / outage
    we didn't observe as one continuous on-window). The first-ever sample
    (last_consenso is None) never counts as a start.
    """
    if last_ts_s is not None and last_consenso == "on":
        delta = now_s - last_ts_s
        if 0 < delta <= 3 * poll_s:
            runtime_s += delta
    if consenso == "on" and last_consenso not in ("on", None):
        cycles += 1
    return runtime_s, cycles


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
        # #6 KPI: accumulate PdC cooling-compressor run-time (consenso_freddo on)
        # + start count, read-only, every cycle incl. deploy-dark. run-time is the
        # efficiency baseline #9 duty-cycling is judged against. `_runtime_base_s`
        # is seeded from the restored sensor value so the total is monotonic across
        # restarts; cycles are since-restart only (a secondary hint).
        self._runtime_base_s = 0.0
        self.cool_runtime_s = 0.0
        self.cool_cycles = 0
        self._last_kpi_ts = None
        self._last_consenso_kpi: str | None = None

    def seed_runtime_base(self, hours: float) -> None:
        """Seed the accumulated run-time from a restored sensor value (hours)."""
        if hours >= 0:
            self._runtime_base_s = hours * 3600.0

    @property
    def cool_runtime_hours(self) -> float:
        return (self._runtime_base_s + self.cool_runtime_s) / 3600.0

    def _accumulate_runtime(self, consenso: str | None) -> None:
        now_s = dt_util.utcnow().timestamp()
        self.cool_runtime_s, self.cool_cycles = runtime_step(
            now_s=now_s,
            last_ts_s=self._last_kpi_ts,
            last_consenso=self._last_consenso_kpi,
            consenso=consenso,
            runtime_s=self.cool_runtime_s,
            cycles=self.cool_cycles,
            poll_s=UPDATE_INTERVAL.total_seconds(),
        )
        self._last_kpi_ts = now_s
        self._last_consenso_kpi = consenso

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
        # KNX temperature objects report cyclically even while the numeric value
        # is unchanged.  HA advances last_reported for those telegrams but leaves
        # last_updated at the time the value/attributes last changed.
        return (dt_util.utcnow() - state.last_reported).total_seconds()

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
        consenso_freddo = self._bin_state(CONSENSO_FREDDO)
        self._accumulate_runtime(consenso_freddo)
        return {
            "speeds": speeds,
            "cooling_zones": cooling_zones,
            "cooling_zone_count": len(cooling_zones),
            "consenso_freddo": consenso_freddo,
            "consenso_caldo": self._bin_state(CONSENSO_CALDO),
            "zone_temps": zone_temps,
            "cool_runtime_hours": self.cool_runtime_hours,
            "cool_cycles": self.cool_cycles,
        }
