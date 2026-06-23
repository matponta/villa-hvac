"""Diagnostic sensors for Villa HVAC (Phase 0, read-only)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaHvacConfigEntry
from .const import ZONES
from .coordinator import VillaHvacCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensor and the per-zone fused temperature sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [CoolingDemandZonesSensor(coordinator, entry)]
    entities += [
        ZoneTemperatureSensor(coordinator, entry, zone_id, zone)
        for zone_id, zone in ZONES.items()
    ]
    async_add_entities(entities)


class CoolingDemandZonesSensor(
    CoordinatorEntity[VillaHvacCoordinator], SensorEntity
):
    """Number of zones currently calling for cooling (fancoil fan > 0)."""

    _attr_has_entity_name = True
    _attr_name = "Cooling demand zones"
    _attr_icon = "mdi:snowflake-thermometer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "zones"

    def __init__(
        self, coordinator: VillaHvacCoordinator, entry: VillaHvacConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cooling_demand_zones"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("cooling_zone_count")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            "zones": data.get("cooling_zones"),
            "consenso_freddo": data.get("consenso_freddo"),
            "consenso_caldo": data.get("consenso_caldo"),
        }


class ZoneTemperatureSensor(
    CoordinatorEntity[VillaHvacCoordinator], SensorEntity
):
    """Fused current temperature for a zone (#1).

    Thermostat-primary: reads the zone's `sensor.clima_*` twin, falling back to
    the climate's `current_temperature` when the primary is missing or stale.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: VillaHvacCoordinator,
        entry: VillaHvacConfigEntry,
        zone_id: str,
        zone: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_name = f"{zone['name']} temperature"
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_temperature"

    @property
    def _zone_data(self) -> dict:
        return (self.coordinator.data.get("zone_temps") or {}).get(self._zone_id) or {}

    @property
    def native_value(self) -> float | None:
        return self._zone_data.get("value")

    @property
    def available(self) -> bool:
        return super().available and self._zone_data.get("value") is not None

    @property
    def extra_state_attributes(self) -> dict:
        data = self._zone_data
        return {
            "source": data.get("source"),
            "sensor_raw": data.get("sensor_raw"),
            "climate_raw": data.get("climate_raw"),
        }
