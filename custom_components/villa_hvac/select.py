"""House-mode select for Villa HVAC (#2a).

The integration-owned source of truth for the house climate mode. Changing it
drives the mapped KNX preset onto every controllable thermostat zone.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaHvacConfigEntry
from .const import HOUSE_MODE_HOME, HOUSE_MODES
from .controller import apply_house_mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the house-mode select."""
    async_add_entities([HouseModeSelect(entry)])


class HouseModeSelect(SelectEntity, RestoreEntity):
    """Casa / Via / Notte / Vacanza house mode (drives KNX presets)."""

    _attr_has_entity_name = True
    _attr_name = "House mode"
    _attr_icon = "mdi:home-thermometer"
    _attr_options = HOUSE_MODES

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_house_mode"
        self._attr_current_option = HOUSE_MODE_HOME

    async def async_added_to_hass(self) -> None:
        """Restore the last selected mode (no preset is re-applied on restore)."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in HOUSE_MODES:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        """Select a mode and apply its preset across the house."""
        self._attr_current_option = option
        self.async_write_ha_state()
        await apply_house_mode(self.hass, self._entry, option)
