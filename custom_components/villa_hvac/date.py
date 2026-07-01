"""Return-home date for Villa HVAC (#8).

The date part of the coarse return ETA (paired with the return-daypart select).
Set by the actionable "when are you back?" notification or the dashboard.
"""
from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import VillaHvacConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the #8 return-home date."""
    async_add_entities([ReturnDate(entry)])


class ReturnDate(DateEntity, RestoreEntity):
    """The date you expect to be back (#8)."""

    _attr_has_entity_name = True
    _attr_name = "Return date"
    _attr_icon = "mdi:calendar-arrow-left"

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_return_date"
        self._attr_native_value: date | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            parsed = dt_util.parse_date(last.state)
            if parsed is not None:
                self._attr_native_value = parsed

    async def async_set_value(self, value: date) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
