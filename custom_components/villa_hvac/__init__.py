"""The Villa HVAC orchestration integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant

from .away import AwayController
from .const import PLATFORMS
from .controller import apply_house_mode, current_house_mode
from .coordinator import VillaHvacCoordinator
from .engine import RoomModelStore, SupervisorEngine
from .night import NightController
from .policies import POLICIES, DutyController, FanBandController
from .window import WindowController

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

    # Window pause (#4): open window -> pause that zone's cooling.
    window = WindowController(hass, entry)
    coordinator.window = window
    window.start()
    entry.async_on_unload(window.stop)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Unified Supervisor loop (the "single organism"). Ticks off the coordinator
    # but writes nothing until the master switch.villa_hvac_supervisor is turned
    # on (deploy-dark). On unload it releases the central cooling block so the
    # villa is never left globally blocked without the supervisor alive.
    # The pure policy stack + the stateful controllers (#9 duty, #3 fan band).
    # They are passed separately so the engine can run the pure policies alone
    # for the read-only #11 plan view without advancing the controllers' timers.
    # F2: load the persisted per-room thermal models and seed the estimator.
    model_store = RoomModelStore(hass)
    model_data = await model_store.async_load()
    engine = SupervisorEngine(
        hass, entry, coordinator,
        policies=POLICIES,
        controllers=(DutyController(), FanBandController()),
        model_store=model_store,
    )
    engine.thermal.load(model_data)
    coordinator.engine = engine
    engine.start()
    entry.async_on_unload(engine.stop)
    entry.async_on_unload(engine.async_fail_safe)

    # Re-apply the restored house mode once HA has fully started (re-enters
    # camere silenziose after a reboot in Notte). Mirrors the legacy
    # clima_risincronizza; no-op if HA is already running (e.g. options reload).
    async def _startup_resync(_event: Event) -> None:
        await apply_house_mode(hass, entry, current_house_mode(hass, entry))

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _startup_resync)
    )
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> None:
    """Reload when options change (night threshold / auto-wake time)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
