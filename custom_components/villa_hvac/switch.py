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
from .engine import shadeable_zones

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
        FanPacingSwitch(entry),
        ReturnPrecondSwitch(entry),
        ReturnArmedSwitch(entry),
        PvBiasSwitch(entry),
        UnifiedPlannerSwitch(entry),
        SplitAcSwitch(entry),
        FreeAirSwitch(entry),
        VmcAutoSwitch(entry),
        FreeCoolSwitch(entry),
        WindowsFreeCoolSwitch(entry),
    ]
    entities += [
        ZoneEnableSwitch(coordinator, entry, zone_id, zone)
        for zone_id, zone in ZONES.items()
        if zone.get("climate") and zone.get("emitter") == "fancoil"
    ]
    entities += [
        ShadeBlockSwitch(entry, zone, name)
        for zone, name in shadeable_zones(hass).items()
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
        # Hand the villa back to native KNX: turning the master off must never
        # leave the villa globally blocked (or a fancoil pinned in manual) with
        # the engine idle — the same invariant the unload fail-safe enforces.
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.async_fail_safe()


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


class FreeAirSwitch(SwitchEntity, RestoreEntity):
    """#3 free-air / windows-open (default OFF).

    A manual "I've opened the windows" flag: while on, the cooled fancoil zones
    are treated as window-paused (building_protection, fan handed back to AUTO,
    band yields) so the AC doesn't fight the open air. Same effect as a #4 window
    contact opening, house-wide. Turning it off re-applies the house mode.
    """

    _attr_has_entity_name = True
    _attr_name = "Free air"
    _attr_icon = "mdi:window-open-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_free_air"
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


class FreeCoolSwitch(SwitchEntity, RestoreEntity):
    """#5 free-cooling auto-coast (default OFF).

    When on (and the master is on): in summer, whenever the outside air drops
    below the free-cool threshold (default 22 °C), active cooling is suppressed
    and the house coasts on the cool air. v0.53.0: was an always-on options
    toggle; now an explicit opt-in — the owner decides whether a cool evening
    pauses the AC. Entity: switch.free_cooling (unique suffix free_cool).
    """

    _attr_has_entity_name = True
    _attr_name = "Free cooling"
    _attr_icon = "mdi:weather-windy"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_free_cool"
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



class WindowsFreeCoolSwitch(SwitchEntity, RestoreEntity):
    """Windows -> free-cooling inference (owner rule 2, default OFF).

    When on (and the master is on): in summer, with at least
    OPT_WINDOWS_FREE_COOL_COUNT window contacts open and the outdoor air at
    least OPT_WINDOWS_FREE_COOL_MARGIN cooler than the house indoor mean, the
    house is being aired deliberately -> the whole cooling stack coasts exactly
    like #5 free-cooling. Entity: switch.windows_free_cooling (unique suffix
    windows_free_cool).
    """

    _attr_has_entity_name = True
    _attr_name = "Windows free cooling"
    _attr_icon = "mdi:window-open-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_windows_free_cool"
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


class VmcAutoSwitch(SwitchEntity, RestoreEntity):
    """#5 VMC auto-boost (default OFF).

    When on (and the master is on), the VMC controller boosts each ventilation
    machine to flush its rooms with cool outdoor air when summer + outside is
    meaningfully cooler than inside (a free night-flush). Edge-triggered, respects
    a manual boost. The actuator is the separate KNX switch.vmc_boost.
    """

    _attr_has_entity_name = True
    _attr_name = "VMC auto"
    _attr_icon = "mdi:fan-plus"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_vmc_auto"
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
        vmc = getattr(self._entry.runtime_data, "vmc", None)
        if vmc is not None:
            await vmc.async_release()  # stop managing -> hand the boost back


class FanPacingSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for #3 fan pacing (default OFF).

    When on (and the master is on), the engine holds each cooling room's fan in
    MANUAL at a paced speed (pull-down then maintain) during a cooling run.
    Off by default — the pacing speeds want the live held-low-fan test first.
    """

    _attr_has_entity_name = True
    _attr_name = "Fan pacing"
    _attr_icon = "mdi:fan-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fan_pacing"
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


class ReturnPrecondSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for #8 return-home pre-conditioning (default OFF).

    When on (and the master is on), a Via with an armed return ETA drives the
    house into deep setback (building_protection) until the pre-cond window, then
    ramps to comfort so it's ready by arrival. Off by default — deploy-dark.
    """

    _attr_has_entity_name = True
    _attr_name = "Return pre-cond"
    _attr_icon = "mdi:home-import-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_return_precond"
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


class ReturnArmedSwitch(SwitchEntity, RestoreEntity):
    """A return-home ETA is armed (#8, default OFF).

    Set by the actionable "when are you back?" notification or the dashboard.
    While off, #8 keeps the house in deep setback with no pre-cond ramp (you told
    it you don't know when you're back). Turning it off means "no ETA".
    """

    _attr_has_entity_name = True
    _attr_name = "Return armed"
    _attr_icon = "mdi:calendar-check"

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_return_armed"
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


class PvBiasSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for PV/energy-aware daily pre-cool (default OFF).

    When on (with the master + fan_pacing on), the engine banks coolth at the
    thermodynamically most effective hours using the solar forecast + battery as a
    buffer, and defers within comfort during the hot/inefficient evening. Works
    purely through the band center — no new lever. Off by default (deploy-dark).
    """

    _attr_has_entity_name = True
    _attr_name = "PV bias"
    _attr_icon = "mdi:solar-power-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pv_bias"
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


class UnifiedPlannerSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for the F4c unified planner to DRIVE the band center (default OFF).

    When on (with the master + fan_pacing on), the forecast schedule's per-room
    center reference drives the band center for planner-ELIGIBLE rooms (k
    converged + solar-excited); every other room keeps the reactive compose_center
    ladder. The reactive band still owns the comfort guarantee (clamps the
    reference into [comfort_floor, duty_comfort_max]). Off by default (deploy-dark)
    — enable only after mild-weather validation + per-room k-convergence.
    """

    _attr_has_entity_name = True
    _attr_name = "Unified planner"
    _attr_icon = "mdi:calendar-arrow-right"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_unified_planner"
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


class SplitAcSwitch(SwitchEntity, RestoreEntity):
    """Opt-in for the #6 split-AC trio controller (default OFF, deploy-dark).

    When on (and the master is on), the SplitGroupController manages the Cantina
    (wine-storage cool) and Palestra (summer occupancy cool) heads on the shared
    Daikin outdoor unit, emitting cool-side only. Garage stays owner-manual. Off
    by default — P0 is observe-only, so this switch actuates nothing until P1.
    """

    _attr_has_entity_name = True
    _attr_name = "Split AC"
    _attr_icon = "mdi:air-conditioner"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_split_ac"
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
        # Hand the heads back to their own thermostat/native state via fail-safe.
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.async_fail_safe()


class ShadeBlockSwitch(SwitchEntity, RestoreEntity):
    """Per-room manual override to block solar shading (#6, default OFF).

    When on, the room's sun-facing blind is skipped by `shading_policy` — it is
    neither closed nor reopened, leaving it under manual/other control. Use it
    when you're in the room and don't want the blind to come down. Read by the
    engine via the cover enrichment; toggling nudges the engine to re-plan.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:window-shutter-cog"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry, zone: str, name: str) -> None:
        self._entry = entry
        self._attr_name = f"{name} shade block"
        self._attr_unique_id = f"{entry.entry_id}_shade_block_{zone}"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def _request_run(self) -> None:
        engine = getattr(self._entry.runtime_data, "engine", None)
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
