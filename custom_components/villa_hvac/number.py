"""House comfort-setpoint slider (#2 enhancement).

A dashboard-friendly number that sets the whole-house comfort temperature. The
house-mode driver pushes `setpoint + mode offset` to every controllable zone, so
this is the single knob for house temperature (no ETS round-trips). Changing it
re-applies the current mode immediately.
"""
from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaHvacConfigEntry
from .const import (
    DEFAULT_HOUSE_SETPOINT,
    HOUSE_SETPOINT_MAX,
    HOUSE_SETPOINT_MIN,
    HOUSE_SETPOINT_STEP,
)
from .controller import apply_house_mode, current_house_mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the house setpoint number."""
    async_add_entities([HouseSetpointNumber(entry)])


class HouseSetpointNumber(NumberEntity, RestoreEntity):
    """Whole-house comfort setpoint; drives set_temperature on mode apply."""

    _attr_has_entity_name = True
    _attr_name = "House setpoint"
    _attr_icon = "mdi:home-thermometer"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = HOUSE_SETPOINT_MIN
    _attr_native_max_value = HOUSE_SETPOINT_MAX
    _attr_native_step = HOUSE_SETPOINT_STEP
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_house_setpoint"
        self._attr_native_value = DEFAULT_HOUSE_SETPOINT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        await apply_house_mode(
            self.hass, self._entry, current_house_mode(self.hass, self._entry)
        )
