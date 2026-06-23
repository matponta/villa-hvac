"""Per-zone enable switch for Villa HVAC (#10 long-term zone disable).

Turning a zone OFF forces its KNX thermostat to the ``building_protection``
preset, which drives the fancoil fan to 0 (cooling consenso drops off after the
KNX off-delay) while keeping frost/building protection active. Turning it back
ON restores the preset that was active before the zone was disabled (falling
back to ``comfort``).

This is an explicit, long-term user command — it is intentionally *not* part of
the automatic occupancy/setback logic (that lands in later increments).
"""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaHvacConfigEntry
from .const import PRESET_BUILDING_PROTECTION, PRESET_DEFAULT_ENABLED, ZONES
from .controller import apply_house_mode, current_house_mode
from .coordinator import VillaHvacCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create an enable switch for each fancoil zone that owns a KNX thermostat.

    Restricted to ``emitter == "fancoil"``: the building_protection -> fan 0
    lever is only verified for fancoils, not radiant or split-AC zones.
    """
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [AutoSetbackSwitch(entry)]
    entities += [
        ZoneEnableSwitch(coordinator, entry, zone_id, zone)
        for zone_id, zone in ZONES.items()
        if zone.get("climate") and zone.get("emitter") == "fancoil"
    ]
    async_add_entities(entities)


class AutoSetbackSwitch(SwitchEntity, RestoreEntity):
    """Global enable for the #2 house-mode setback automation (default ON).

    When off, changing the house mode writes nothing. Turning it on re-applies
    the current mode immediately.
    """

    _attr_has_entity_name = True
    _attr_name = "Auto setback"
    _attr_icon = "mdi:home-clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_auto_setback"
        self._attr_is_on = True  # opt-out, not opt-in

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        # force=True: our own state write may not be readable back yet.
        await apply_house_mode(
            self.hass, self._entry, current_house_mode(self.hass, self._entry),
            force=True,
        )

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class ZoneEnableSwitch(
    CoordinatorEntity[VillaHvacCoordinator], SwitchEntity, RestoreEntity
):
    """Enable/disable a zone. Off -> force building_protection (frost-safe)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:home-thermometer"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: VillaHvacCoordinator,
        entry: VillaHvacConfigEntry,
        zone_id: str,
        zone: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._climate: str = zone["climate"]
        self._attr_name = f"{zone['name']} enabled"
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_enabled"
        # Default enabled; corrected from restored state in async_added_to_hass.
        self._attr_is_on = True
        self._preset_before_disable: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the enabled/disabled flag across restarts.

        We only restore the flag — we do not re-issue the preset service on
        startup, to avoid fighting whatever the KNX thermostats already hold.
        """
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    def _current_preset(self) -> str | None:
        """Read the preset the KNX thermostat is currently in, if available."""
        state = self.hass.states.get(self._climate)
        if state is None:
            return None
        return state.attributes.get(ATTR_PRESET_MODE)

    async def _async_set_preset(self, preset: str) -> None:
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: self._climate, ATTR_PRESET_MODE: preset},
            blocking=True,
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Re-enable the zone: restore the preset captured at disable time."""
        preset = self._preset_before_disable or PRESET_DEFAULT_ENABLED
        await self._async_set_preset(preset)
        self._preset_before_disable = None
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the zone: force building_protection, keep frost protection."""
        # Capture the current preset so we can restore it on re-enable, but
        # never capture building_protection itself.
        current = self._current_preset()
        if current and current != PRESET_BUILDING_PROTECTION:
            self._preset_before_disable = current
        await self._async_set_preset(PRESET_BUILDING_PROTECTION)
        self._attr_is_on = False
        self.async_write_ha_state()
