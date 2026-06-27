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

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VillaHvacConfigEntry
from .const import ZONES
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
    entities: list[SwitchEntity] = [
        AutoSetbackSwitch(entry),
        SupervisorEnableSwitch(entry),
        DutyCycleSwitch(entry),
    ]
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


class SupervisorEnableSwitch(SwitchEntity, RestoreEntity):
    """Master enable for the unified Supervisor loop (default OFF = deploy-dark).

    While off, the Supervisor engine builds state but writes nothing. This lets
    us deploy the integration and light the organism up deliberately, one step
    at a time, on the live house. The legacy/standalone controllers (#2/#4/#10)
    are unaffected by this switch until they are migrated onto the engine.
    """

    _attr_has_entity_name = True
    _attr_name = "Supervisor"
    _attr_icon = "mdi:home-automation"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_supervisor"
        self._attr_is_on = False  # opt-in: nothing actuates until enabled

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        # Light up immediately rather than waiting for the next 30 s tick.
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class DutyCycleSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for the #9 central duty-cycle (default OFF).

    When on (and the master is on), the engine caps the villa's continuous
    cooling stint at the configured max and then forces a cooloff via the
    Consenso BLOCCO. Off by default — and BLOCCO actuation additionally requires
    the master switch, so this stays dark until you deliberately turn both on.
    """

    _attr_has_entity_name = True
    _attr_name = "Duty cycle"
    _attr_icon = "mdi:sine-wave"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_duty_cycle"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()


class ZoneEnableSwitch(
    CoordinatorEntity[VillaHvacCoordinator], SwitchEntity, RestoreEntity
):
    """Enable/disable a zone (#10). The flag is read by the engine's
    `disabled_zones_policy`, which forces building_protection while off and lets
    `house_mode_policy` restore the mode preset on re-enable. This entity only
    flips the flag and nudges the engine — no direct service writes.
    """

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
        self._attr_name = f"{zone['name']} enabled"
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_enabled"
        # Default enabled; corrected from restored state in async_added_to_hass.
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        """Restore the enabled/disabled flag across restarts (no preset reissue)."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    async def _request_run(self) -> None:
        engine = getattr(self.coordinator, "engine", None)
        if engine is not None:
            await engine.request_run()

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._request_run()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._request_run()
