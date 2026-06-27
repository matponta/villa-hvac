"""Supervisor engine — the Home-Assistant-facing half of the organism (Phase A).

Builds the per-cycle `HouseState`, runs the priority policy stack, and applies
the merged result through the pure write-arbiter (`supervisor.reconcile`), one
lever at a time, idempotently. The control discipline itself is in
`supervisor.py` (pure, unit-tested); this module is the I/O shell: read state,
call services, gate on the master enable switch, and the fail-safe.

A2 wires the skeleton: the loop runs each coordinator tick but ships with an
empty policy list (no actuation) behind a master switch that defaults OFF
(deploy-dark). Policies are migrated in onto this loop in A3/A4.
"""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
)
from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    DOMAIN as FAN_DOMAIN,
    SERVICE_SET_PERCENTAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_CLOSED,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONSENSO_BLOCCO,
    COOL_VALVES,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SHADING_SOLAR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_SHADING_ENABLED,
    OPT_SHADING_SOLAR,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    SHADING_ORIENTATIONS,
    SHADING_SKIP_AREAS,
    SOLAR_RADIATION,
    ZONES,
)
from .controller import (
    auto_setback_enabled,
    current_house_mode,
    current_house_setpoint,
    current_season,
    is_zone_disabled,
    mode_offset,
    supervisor_enabled,
)
from .supervisor import (
    BLOCCO_LEVER,
    CoverInfo,
    HouseState,
    LeverState,
    ZoneSnapshot,
    merge_desired,
    reconcile,
)

_LOGGER = logging.getLogger(__name__)


def _num(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a sensor whose state is a number (None if missing/non-numeric)."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _outdoor_temp(hass: HomeAssistant) -> float | None:
    """Ecowitt outdoor temp, falling back to the PdC's own probe."""
    val = _num(hass, OUTDOOR_TEMP)
    return val if val is not None else _num(hass, OUTDOOR_TEMP_FALLBACK)


def shadeable_covers(hass: HomeAssistant) -> tuple[CoverInfo, ...]:
    """Resolve shadeable covers from the registries (#6) — not hardcoded.

    Per cover: zone = entity.area_id else device.area_id; orientation =
    (entity.labels ∪ device.labels) ∩ {north,east,south,west}; floor =
    area.floor_id. Covers with no area / an unassigned area (the orphan
    `cover.tapparella`) or no orientation label are skipped.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    covers: list[CoverInfo] = []
    for entity in ent_reg.entities.values():
        if entity.domain != "cover":
            continue
        labels = set(entity.labels or ())
        area_id = entity.area_id
        if entity.device_id and (device := dev_reg.async_get(entity.device_id)):
            labels |= set(device.labels or ())
            if area_id is None:
                area_id = device.area_id
        if not area_id or area_id in SHADING_SKIP_AREAS:
            continue
        orientation = next((lbl for lbl in labels if lbl in SHADING_ORIENTATIONS), None)
        if orientation is None:
            continue
        area = area_reg.async_get_area(area_id)
        covers.append(
            CoverInfo(
                entity_id=entity.entity_id,
                orientation=orientation,
                zone=area_id,
                floor=area.floor_id if area else None,
            )
        )
    return tuple(covers)


def build_house_state(
    hass: HomeAssistant, entry: ConfigEntry, coordinator
) -> HouseState:
    """Assemble one unified snapshot of the house for the policy stack."""
    data = coordinator.data or {}
    zone_temps = data.get("zone_temps") or {}
    window = getattr(coordinator, "window", None)
    paused = window.paused if window is not None else set()

    zones: dict[str, ZoneSnapshot] = {}
    for zone_id, zone in ZONES.items():
        valve = COOL_VALVES.get(zone_id)
        demand: bool | None = None
        if valve is not None and (vs := hass.states.get(valve)) is not None:
            if vs.state in (STATE_ON, STATE_OFF):
                demand = vs.state == STATE_ON
        zones[zone_id] = ZoneSnapshot(
            zone_id=zone_id,
            name=zone["name"],
            climate=zone.get("climate"),
            emitter=zone.get("emitter"),
            temp=(zone_temps.get(zone_id) or {}).get("value"),
            demand=demand,
            enabled=not is_zone_disabled(hass, entry, zone_id),
            paused=zone_id in paused,
        )

    blocco_state = hass.states.get(CONSENSO_BLOCCO)
    mode = current_house_mode(hass, entry)
    sun = hass.states.get("sun.sun")
    return HouseState(
        now=dt_util.utcnow(),
        zones=zones,
        covers=shadeable_covers(hass),
        sun_azimuth=sun.attributes.get("azimuth") if sun else None,
        sun_elevation=sun.attributes.get("elevation") if sun else None,
        shading_enabled=bool(
            entry.options.get(OPT_SHADING_ENABLED, DEFAULT_SHADING_ENABLED)
        ),
        shading_solar_threshold=float(
            entry.options.get(OPT_SHADING_SOLAR, DEFAULT_SHADING_SOLAR)
        ),
        season=current_season(hass, entry),
        house_mode=mode,
        auto_setback=auto_setback_enabled(hass, entry),
        house_setpoint=current_house_setpoint(hass, entry),
        mode_offset=mode_offset(hass, entry, mode),
        free_cool_enabled=bool(
            entry.options.get(OPT_FREE_COOL_ENABLED, DEFAULT_FREE_COOL_ENABLED)
        ),
        free_cool_threshold=float(
            entry.options.get(OPT_FREE_COOL_OUTDOOR, DEFAULT_FREE_COOL_OUTDOOR)
        ),
        outdoor_temp=_outdoor_temp(hass),
        solar=_num(hass, SOLAR_RADIATION),
        consenso_freddo=data.get("consenso_freddo"),
        consenso_caldo=data.get("consenso_caldo"),
        blocco=blocco_state.state if blocco_state is not None else None,
    )


class SupervisorEngine:
    """Runs the policy stack each coordinator tick and applies the result.

    `policies` is the ordered (HIGH→LOW priority) list of callables
    `(HouseState) -> dict[lever_key, value]`. A2 ships an empty list; the
    existing controllers keep running until they are migrated onto this loop.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator,
        policies=None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.policies = list(policies or [])
        self._lever_states: dict[str, LeverState] = {}
        self._unsub = None
        self._busy = False

    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self._on_update)

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # -- enable gate ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True only when the master switch is explicitly on (deploy-dark)."""
        return supervisor_enabled(self.hass, self.entry)

    # -- main loop ------------------------------------------------------------
    @callback
    def _on_update(self) -> None:
        if not self.enabled or self._busy:
            return
        self.hass.async_create_task(self._run())

    async def request_run(self) -> None:
        """Run a supervisor pass immediately (event-driven responsiveness).

        Lets event sources — a window opening, a mode change, a zone toggle —
        get an instant pass instead of waiting for the 30 s tick, while the
        engine stays the single writer. No-op while disabled or mid-pass.
        """
        if self.enabled and not self._busy:
            await self._run()

    async def _run(self) -> None:
        self._busy = True
        try:
            state = build_house_state(self.hass, self.entry, self.coordinator)
            outputs = [policy(state) for policy in self.policies]
            desired = merge_desired(outputs)
            for lever, target in desired.items():
                await self._reconcile_lever(lever, target, state)
        finally:
            self._busy = False

    async def _reconcile_lever(self, lever: str, target, state: HouseState) -> None:
        current = self._read_current(lever)
        result = reconcile(
            target, current, self._lever_states.get(lever, LeverState()), state.now
        )
        self._lever_states[lever] = result.state
        if result.write is not None:
            await self._dispatch_write(lever, result.write)

    # -- lever I/O ------------------------------------------------------------
    def _read_current(self, lever: str):
        if lever == BLOCCO_LEVER:
            s = self.hass.states.get(CONSENSO_BLOCCO)
            return s.state if s is not None else None
        kind, _, entity = lever.partition(":")
        s = self.hass.states.get(entity)
        if s is None:
            return None
        if kind == "preset":
            return s.attributes.get(ATTR_PRESET_MODE)
        if kind == "temperature":
            return s.attributes.get(ATTR_TEMPERATURE)
        if kind == "fan":
            return s.attributes.get(ATTR_PERCENTAGE)
        if kind == "cover":
            return s.state  # open / closed / opening / closing
        return None

    async def _dispatch_write(self, lever: str, value) -> None:
        if lever == BLOCCO_LEVER:
            await self._call_switch(CONSENSO_BLOCCO, on=str(value) == STATE_ON)
            return
        kind, _, entity = lever.partition(":")
        if kind == "preset":
            await self._call(
                CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE,
                {ATTR_ENTITY_ID: entity, ATTR_PRESET_MODE: value},
            )
        elif kind == "temperature":
            await self._call(
                CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE,
                {ATTR_ENTITY_ID: entity, ATTR_TEMPERATURE: float(value)},
            )
        elif kind == "fan":
            await self._call(
                FAN_DOMAIN, SERVICE_SET_PERCENTAGE,
                {ATTR_ENTITY_ID: entity, ATTR_PERCENTAGE: int(float(value))},
            )
        elif kind == "cover" and str(value) == STATE_CLOSED:
            await self._call(
                COVER_DOMAIN, SERVICE_CLOSE_COVER, {ATTR_ENTITY_ID: entity}
            )

    async def _call(self, domain: str, service: str, data: dict) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _call_switch(self, entity_id: str, *, on: bool) -> None:
        await self._call(
            "switch", SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
        )

    # -- fail-safe ------------------------------------------------------------
    async def async_fail_safe(self) -> None:
        """Release the central cooling block so we never leave the villa stuck.

        Invariant: the villa must never stay globally blocked without the
        supervisor alive. (As policies that force per-zone presets are migrated
        in, this will also hand those zones back.)
        """
        state = self.hass.states.get(CONSENSO_BLOCCO)
        if state is not None and state.state == STATE_ON:
            try:
                await self._call_switch(CONSENSO_BLOCCO, on=False)
            except Exception:  # noqa: BLE001 - fail-safe must not raise on unload
                _LOGGER.exception("Fail-safe: could not release %s", CONSENSO_BLOCCO)
