"""The Villa HVAC orchestration integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback

from .away import AwayController
from .const import PLATFORMS
from .controller import apply_house_mode, current_house_mode
from .coordinator import VillaHvacCoordinator
from .engine import RoomModelStore, SupervisorEngine
from .night import NightSilenceController
from .policies import POLICIES, CoolingController, SplitGroupController
from .returnhome import ReturnHomeManager
from .window import WindowController

# Typed config entry (HA 2024.6+): coordinator lives in entry.runtime_data
VillaHvacConfigEntry = ConfigEntry[VillaHvacCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> bool:
    """Set up Villa HVAC from a config entry."""
    coordinator = VillaHvacCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Camere silenziose controller (#2b, C1): an engine merge controller. Attached
    # to the coordinator (build_house_state reads its wake latch) and added to the
    # engine's controllers below so its bedroom writes flow through the arbiter.
    night = NightSilenceController(hass, entry, coordinator)
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
        # Tier-1 M1: ONE CoolingController (regime + duty + band folded).
        # NightSilenceController LAST: on the Notte-exit cycle its one-shot manuale
        # release must yield to the band re-taking a bedroom for pacing.
        # SplitGroupController (#6): disjoint lever set (aircon_* hvac_mode/temp/
        # fan_mode) — merge order immaterial; never touches the PdC/BLOCCO stack.
        controllers=(CoolingController(), night, SplitGroupController()),
        model_store=model_store,
    )
    engine.thermal.load(model_data)
    coordinator.engine = engine
    engine.start()
    entry.async_on_unload(engine.stop)
    entry.async_on_unload(engine.async_fail_safe)

    # Safety hooks that config-entry *unload* does not cover:
    #  - HA shutdown/reboot fires EVENT_HOMEASSISTANT_STOP but does NOT run the
    #    async_on_unload callbacks, so without this a live BLOCCO block would be
    #    orphaned across the outage with no supervisor alive to release it.
    async def _on_stop(_event: Event) -> None:
        await engine.async_fail_safe()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    )
    #  - Boot-time safe baseline: release the central cooling block regardless of
    #    the master switch. Deploy-dark means "don't optimise", not "leave the
    #    villa unable to cool" — a block stranded across a crash/restart (with the
    #    master off) would otherwise persist until a human noticed.
    await engine.async_release_blocco()

    # #8 return-home: ask "when are you back?" on the Via transition and map the
    # answer onto the entities. Started after the platforms so the house-mode
    # select entity is registered. The actuation (deep setback -> pre-cond ramp)
    # lives in the engine's AwayReturnController; this is only the trigger.
    returns = ReturnHomeManager(hass, entry)
    returns.start()
    entry.async_on_unload(returns.stop)

    # Re-apply the restored house mode once HA has fully started (re-enters
    # camere silenziose after a reboot in Notte). Mirrors the legacy
    # clima_risincronizza; no-op if HA is already running (e.g. options reload).
    _started_unsub: CALLBACK_TYPE | None = None

    async def _startup_resync(_event: Event) -> None:
        # Re-attempt the safe baseline now that HA (and the KNX integration that
        # owns the block entity) is fully up — the setup-time release is skipped
        # when that entity isn't loaded yet on a cold boot.
        nonlocal _started_unsub
        # The once-listener auto-removes as it fires; drop our handle so the
        # unload cleanup below can't unsubscribe it a second time (HA logs an
        # ERROR — "unknown job listener" — for removing an already-gone one).
        _started_unsub = None
        await engine.async_release_blocco()
        await apply_house_mode(hass, entry, current_house_mode(hass, entry))

    _started_unsub = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STARTED, _startup_resync
    )

    @callback
    def _cancel_startup_resync() -> None:
        if _started_unsub is not None:
            _started_unsub()

    entry.async_on_unload(_cancel_startup_resync)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> None:
    """Reload when options change (night threshold / auto-wake time)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VillaHvacConfigEntry) -> bool:
    """Unload a config entry."""
    engine = getattr(entry.runtime_data, "engine", None)
    try:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    finally:
        # Belt-and-suspenders: guarantee the safety release even on a partial
        # unload. If a platform fails to unload, async_unload_platforms returns
        # False and HA does NOT run the async_on_unload fail-safe — so run it
        # here too. Idempotent with that callback on the success path.
        if engine is not None:
            await engine.async_fail_safe()
