"""Diagnostic sensors for Villa HVAC (Phase 0, read-only)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaHvacConfigEntry
from .const import DOMAIN
from .coordinator import VillaHvacCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities([CoolingDemandZonesSensor(coordinator, entry)])


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
