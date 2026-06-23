"""The Villa HVAC orchestration integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .away import AwayController
from .const import PLATFORMS
from .coordinator import VillaHvacCoordinator
from .night import NightController

# Typed config entry (HA 2024.6+): coordinator lives in entry.runtime_data
VillaHvacConfigEntry = ConfigEntry[VillaHvacCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> bool:
    """Set up Villa HVAC from a config entry."""
    coordinator = VillaHvacCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Camere silenziose controller (#2b); attached to the coordinator so the
    # house-mode driver can reach it via entry.runtime_data.
    night = NightController(hass, entry, coordinator)
    coordinator.night = night
    night.start()
    entry.async_on_unload(night.stop)

    # Away auto-escalation (#2c): presence -> Via / Casa.
    away = AwayController(hass, entry)
    away.start()
    entry.async_on_unload(away.stop)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> None:
    """Reload when options change (night threshold / auto-wake time)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
