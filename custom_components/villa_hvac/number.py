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
    DEFAULT_FAN_MIN,
    DEFAULT_HOUSE_SETPOINT,
    DEFAULT_SETPOINT_OFFSET,
    DEFAULT_SHADING_POSITION,
    HOUSE_SETPOINT_MAX,
    HOUSE_SETPOINT_MIN,
    HOUSE_SETPOINT_STEP,
    SETPOINT_OFFSET_MAX,
    SETPOINT_OFFSET_MIN,
    SETPOINT_OFFSET_STEP,
    SHADE_POSITION_MAX,
    SHADE_POSITION_MIN,
    SHADE_POSITION_STEP,
    ZONES,
)
from .controller import apply_house_mode, current_house_mode
from .engine import shadeable_zones


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """House setpoint + per-room shade position (#6) + per-zone fan-min (#3 v2)."""
    entities: list[NumberEntity] = [HouseSetpointNumber(entry)]
    entities += [
        ShadePositionNumber(entry, zone, name)
        for zone, name in shadeable_zones(hass).items()
    ]
    # Per-zone min-circulation override for the cooling fancoil leader zones.
    entities += [
        FanMinNumber(entry, zone_id, zone["name"])
        for zone_id, zone in ZONES.items()
        if zone.get("climate") and zone.get("emitter") == "fancoil"
    ]
    # #2: per-zone comfort offset for the cooling fancoil zones.
    entities += [
        ZoneOffsetNumber(entry, zone_id, zone["name"])
        for zone_id, zone in ZONES.items()
        if zone.get("climate") and zone.get("emitter") == "fancoil"
    ]
    async_add_entities(entities)


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


class ShadePositionNumber(NumberEntity, RestoreEntity):
    """Per-room shade target position (#6).

    The HA cover position (0 = fully closed/down, 100 = open) the room's
    sun-facing blind is driven to when shading triggers — so being home doesn't
    mean every blind slams fully shut. Read by the engine's `shading_policy`;
    setting it nudges the engine to re-plan. Defaults to the gentle house default.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:window-shutter"
    _attr_native_min_value = SHADE_POSITION_MIN
    _attr_native_max_value = SHADE_POSITION_MAX
    _attr_native_step = SHADE_POSITION_STEP
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: VillaHvacConfigEntry, zone: str, name: str) -> None:
        self._entry = entry
        self._attr_name = f"{name} shade position"
        self._attr_unique_id = f"{entry.entry_id}_shade_position_{zone}"
        self._attr_native_value = DEFAULT_SHADING_POSITION

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
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()


class ZoneOffsetNumber(NumberEntity, RestoreEntity):
    """Per-zone comfort offset (#2).

    °C added to this room's base center (house setpoint + season/mode offset):
    negative = this room runs cooler than the house, positive = warmer. The house
    slider still moves the whole house; this trims one room relative to it and
    survives mode changes (it stacks on the season offset). Read by the engine's
    band + house-mode policies; setting it nudges a re-plan.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-plus"
    _attr_native_min_value = SETPOINT_OFFSET_MIN
    _attr_native_max_value = SETPOINT_OFFSET_MAX
    _attr_native_step = SETPOINT_OFFSET_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: VillaHvacConfigEntry, zone: str, name: str) -> None:
        self._entry = entry
        self._attr_name = f"{name} offset"
        self._attr_unique_id = f"{entry.entry_id}_setpoint_offset_{zone}"
        self._attr_native_value = DEFAULT_SETPOINT_OFFSET

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
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()


class FanMinNumber(NumberEntity, RestoreEntity):
    """Per-zone min-circulation fan % (#3 v2).

    The fan level held during a REST (valve closed) phase: 0 = fan off (silent),
    higher = gentle continuous circulation. Read by the engine's band controller;
    overrides the global default for this zone. Setting it nudges a re-plan.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:fan-chevron-down"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: VillaHvacConfigEntry, zone: str, name: str) -> None:
        self._entry = entry
        self._attr_name = f"{name} fan min"
        self._attr_unique_id = f"{entry.entry_id}_fan_min_{zone}"
        self._attr_native_value = DEFAULT_FAN_MIN

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
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
