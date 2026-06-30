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

from dataclasses import replace
from datetime import timedelta
import logging

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_SET_COVER_POSITION,
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
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_COOLOFF,
    DEFAULT_DUTY_MAX_STINT,
    DEFAULT_DUTY_PEAK_OUTDOOR,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_PRECOOL_LOOKAHEAD_HOURS,
    DEFAULT_PRECOOL_MARGIN,
    DEFAULT_PRECOOL_OFFSET,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SHADING_POSITION,
    DEFAULT_SHADING_SOLAR,
    FORECAST_REFRESH,
    OPT_DUTY_COMFORT_MAX,
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_DUTY_PEAK_OUTDOOR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_PRECOOL_LOOKAHEAD_HOURS,
    OPT_PRECOOL_MARGIN,
    OPT_PRECOOL_OFFSET,
    OPT_SHADING_DEFAULT_POSITION,
    OPT_SHADING_ENABLED,
    OPT_SHADING_SOLAR,
    OPT_WEATHER_ENTITY,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    SHADE_POSITION_TOLERANCE,
    SHADING_ORIENTATIONS,
    SHADING_SKIP_AREAS,
    SOLAR_RADIATION,
    WEATHER_ENTITY_DEFAULT,
    ZONES,
)
from .controller import (
    auto_setback_enabled,
    current_house_mode,
    current_house_setpoint,
    current_season,
    duty_cycle_enabled,
    fan_pacing_enabled,
    is_zone_disabled,
    mode_offset,
    shade_blocked,
    shade_position,
    supervisor_enabled,
)
from .policies import DutyController
from .supervisor import (
    BLOCCO_LEVER,
    CoverInfo,
    DEFAULT_SETPOINT_TOLERANCE,
    DutyState,
    HouseState,
    LeverState,
    PlanView,
    RunPlan,
    ZoneSnapshot,
    build_plan,
    merge_desired,
    plan_run,
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


def _lookahead(entry: ConfigEntry) -> timedelta:
    """The #9 planner lookahead horizon (default 12 h)."""
    return timedelta(
        hours=float(
            entry.options.get(
                OPT_PRECOOL_LOOKAHEAD_HOURS, DEFAULT_PRECOOL_LOOKAHEAD_HOURS
            )
        )
    )


def _make_run_plan(
    entry: ConfigEntry, forecast, now, outdoor: float | None
) -> RunPlan:
    """Build the #9 forecast run-plan from the cached forecast + options.

    Shared by `build_house_state` (which keeps only `.precool`) and the #11 plan
    view (which surfaces the full plan), so both reason over the same horizon.
    """
    return plan_run(
        list(forecast),
        now,
        outdoor,
        peak_threshold=float(
            entry.options.get(OPT_DUTY_PEAK_OUTDOOR, DEFAULT_DUTY_PEAK_OUTDOOR)
        ),
        lookahead=_lookahead(entry),
        margin=float(entry.options.get(OPT_PRECOOL_MARGIN, DEFAULT_PRECOOL_MARGIN)),
    )


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


def shadeable_zones(hass: HomeAssistant) -> dict[str, str]:
    """Distinct rooms (area_id -> friendly name) that own a shadeable cover (#6).

    The per-room shade-position number + shade-block switch entities are created
    one per zone returned here; the engine keys their lookups by the same zone.
    """
    area_reg = ar.async_get(hass)
    zones: dict[str, str] = {}
    for cover in shadeable_covers(hass):
        if not cover.zone or cover.zone in zones:
            continue
        area = area_reg.async_get_area(cover.zone)
        zones[cover.zone] = area.name if area else cover.zone
    return zones


def build_house_state(
    hass: HomeAssistant, entry: ConfigEntry, coordinator, forecast=()
) -> HouseState:
    """Assemble one unified snapshot of the house for the policy stack.

    `forecast` is the cached hourly forecast (list of (datetime, temp)); the
    engine refreshes it out-of-band and passes it in so this stays sync + pure.
    """
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
        fancoils = zone.get("fancoils") or []
        fancoil = fancoils[0] if fancoils else None
        # manuale switch: explicit in ZONES (bedrooms) else derived from the fan
        # entity (verified naming: fan.fancoil_X -> switch.fancoil_X_manuale).
        manuale = zone.get("manuale_switch")
        if manuale is None and fancoil:
            manuale = "switch." + fancoil.removeprefix("fan.") + "_manuale"
        zones[zone_id] = ZoneSnapshot(
            zone_id=zone_id,
            name=zone["name"],
            climate=zone.get("climate"),
            emitter=zone.get("emitter"),
            temp=(zone_temps.get(zone_id) or {}).get("value"),
            demand=demand,
            enabled=not is_zone_disabled(hass, entry, zone_id),
            paused=zone_id in paused,
            bedroom=bool(zone.get("bedroom")),
            fancoil=fancoil,
            manuale=manuale,
        )

    # #6: enrich each shadeable cover with its room's shade target + block flag.
    shading_default = int(
        entry.options.get(OPT_SHADING_DEFAULT_POSITION, DEFAULT_SHADING_POSITION)
    )
    covers = tuple(
        replace(
            c,
            target_position=shade_position(hass, entry, c.zone) if c.zone else None,
            blocked=shade_blocked(hass, entry, c.zone) if c.zone else False,
        )
        for c in shadeable_covers(hass)
    )

    blocco_state = hass.states.get(CONSENSO_BLOCCO)
    mode = current_house_mode(hass, entry)
    sun = hass.states.get("sun.sun")
    night = getattr(coordinator, "night", None)
    now = dt_util.utcnow()
    outdoor = _outdoor_temp(hass)
    plan = _make_run_plan(entry, forecast, now, outdoor)
    return HouseState(
        now=now,
        zones=zones,
        covers=covers,
        sun_azimuth=sun.attributes.get("azimuth") if sun else None,
        sun_elevation=sun.attributes.get("elevation") if sun else None,
        shading_enabled=bool(
            entry.options.get(OPT_SHADING_ENABLED, DEFAULT_SHADING_ENABLED)
        ),
        shading_solar_threshold=float(
            entry.options.get(OPT_SHADING_SOLAR, DEFAULT_SHADING_SOLAR)
        ),
        shading_default_position=shading_default,
        duty_enabled=duty_cycle_enabled(hass, entry),
        duty_max_stint=timedelta(
            minutes=float(entry.options.get(OPT_DUTY_MAX_STINT, DEFAULT_DUTY_MAX_STINT))
        ),
        duty_cooloff=timedelta(
            minutes=float(entry.options.get(OPT_DUTY_COOLOFF, DEFAULT_DUTY_COOLOFF))
        ),
        duty_comfort_max=float(
            entry.options.get(OPT_DUTY_COMFORT_MAX, DEFAULT_DUTY_COMFORT_MAX)
        ),
        duty_peak_outdoor=float(
            entry.options.get(OPT_DUTY_PEAK_OUTDOOR, DEFAULT_DUTY_PEAK_OUTDOOR)
        ),
        precool=plan.precool,
        precool_offset=float(
            entry.options.get(OPT_PRECOOL_OFFSET, DEFAULT_PRECOOL_OFFSET)
        ),
        night_active=bool(getattr(night, "active", False)),
        fan_pacing_enabled=fan_pacing_enabled(hass, entry),
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
        outdoor_temp=outdoor,
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
        controllers=None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        # `policies` are PURE (functions of HouseState); `controllers` are the
        # stateful ones (#9 duty, #3 pacing) whose internal timers advance only
        # when we actuate. The plan view (#11) runs the pure policies only, so
        # building it never advances those timers — safe to compute while dark.
        self.policies = list(policies or [])
        self.controllers = list(controllers or [])
        self._duty_controller = next(
            (c for c in self.controllers if isinstance(c, DutyController)), None
        )
        self._lever_states: dict[str, LeverState] = {}
        self._unsub = None
        self._busy = False
        self._forecast: list[tuple] = []
        self._forecast_ts = None
        self._plan_view: PlanView | None = None

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

    @property
    def plan_view(self) -> PlanView | None:
        """The latest #11 plan projection (refreshed each cycle, even dark)."""
        return self._plan_view

    # -- main loop ------------------------------------------------------------
    @callback
    def _on_update(self) -> None:
        # Always tick: the plan view (#11) is computed every cycle, even while
        # deploy-dark, so the dashboard can show the organism's intent before we
        # light up actuation. Actuation itself stays gated on the master switch.
        if self._busy:
            return
        self.hass.async_create_task(self._tick())

    async def request_run(self) -> None:
        """Run a supervisor pass immediately (event-driven responsiveness).

        Lets event sources — a window opening, a mode change, a zone toggle —
        get an instant pass instead of waiting for the 30 s tick, while the
        engine stays the single writer. No-op while disabled or mid-pass.
        """
        if self.enabled and not self._busy:
            await self._run()

    async def _tick(self) -> None:
        """Periodic pass: refresh the plan always, actuate only when enabled."""
        await self._cycle(actuate=self.enabled)

    async def _run(self) -> None:
        """Force an actuating pass (event-driven + tests). Also refreshes plan."""
        await self._cycle(actuate=True)

    async def _cycle(self, *, actuate: bool) -> None:
        self._busy = True
        try:
            await self._maybe_refresh_forecast()
            state = build_house_state(
                self.hass, self.entry, self.coordinator, forecast=self._forecast
            )
            # Pure policies first — used both for the plan view and (merged with
            # the stateful controllers) for actuation.
            pure_outputs = [policy(state) for policy in self.policies]
            self._plan_view = self._build_plan_view(state, pure_outputs)
            if actuate:
                ctrl_outputs = [c(state) for c in self.controllers]
                desired = merge_desired([*pure_outputs, *ctrl_outputs])
                for lever, target in desired.items():
                    await self._reconcile_lever(lever, target, state)
        finally:
            self._busy = False

    def _build_plan_view(self, state: HouseState, pure_outputs) -> PlanView:
        """Project the current state into the #11 plan view (read-only).

        Reuses the already-computed pure-policy outputs (preset/setpoint/cover
        opinions) and the live DutyState; never runs the stateful controllers,
        so it has no side effects and is safe to compute while deploy-dark.
        """
        desired = merge_desired(pure_outputs)
        run_plan = _make_run_plan(
            self.entry, self._forecast, state.now, state.outdoor_temp
        )
        duty_state = (
            self._duty_controller.duty if self._duty_controller else DutyState()
        )
        return build_plan(
            state, run_plan, desired, duty_state,
            list(self._forecast), _lookahead(self.entry),
        )

    async def _reconcile_lever(self, lever: str, target, state: HouseState) -> None:
        current = self._read_current(lever)
        # Cover position is numeric and never lands exactly on the commanded value,
        # so it gets a wider tolerance than a setpoint.
        tolerance = (
            SHADE_POSITION_TOLERANCE
            if lever.startswith("cover:")
            else DEFAULT_SETPOINT_TOLERANCE
        )
        result = reconcile(
            target, current, self._lever_states.get(lever, LeverState()), state.now,
            tolerance=tolerance,
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
            # position-controlled shading: compare on current_position (0-100).
            return s.attributes.get(ATTR_CURRENT_POSITION)
        if kind == "switch":
            return s.state  # on / off (e.g. a fancoil manuale switch)
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
        elif kind == "cover":
            # Numeric target -> drive to that position; legacy "closed" -> close.
            if str(value) == STATE_CLOSED:
                await self._call(
                    COVER_DOMAIN, SERVICE_CLOSE_COVER, {ATTR_ENTITY_ID: entity}
                )
            else:
                await self._call(
                    COVER_DOMAIN, SERVICE_SET_COVER_POSITION,
                    {ATTR_ENTITY_ID: entity, ATTR_POSITION: int(float(value))},
                )
        elif kind == "switch":
            await self._call_switch(entity, on=str(value) == STATE_ON)

    async def _call(self, domain: str, service: str, data: dict) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _call_switch(self, entity_id: str, *, on: bool) -> None:
        await self._call(
            "switch", SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
        )

    # -- forecast (#9 planner) ------------------------------------------------
    def _weather_entity(self) -> str | None:
        """Configured weather entity, else the default, else any weather.*."""
        configured = self.entry.options.get(OPT_WEATHER_ENTITY) or WEATHER_ENTITY_DEFAULT
        if self.hass.states.get(configured) is not None:
            return configured
        for eid in self.hass.states.async_entity_ids("weather"):
            return eid
        return None

    async def _maybe_refresh_forecast(self) -> None:
        """Re-fetch the hourly forecast at most every FORECAST_REFRESH; best-effort."""
        now = dt_util.utcnow()
        if self._forecast_ts is not None and now - self._forecast_ts < FORECAST_REFRESH:
            return
        self._forecast_ts = now  # stamp first so a failure doesn't hammer the service
        entity = self._weather_entity()
        if not entity:
            self._forecast = []
            return
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"type": "hourly", ATTR_ENTITY_ID: entity},
                blocking=True, return_response=True,
            )
        except Exception:  # noqa: BLE001 - forecast is best-effort, never fatal
            _LOGGER.debug("Forecast fetch failed for %s", entity, exc_info=True)
            return
        entries = (resp or {}).get(entity, {}).get("forecast") or []
        parsed: list[tuple] = []
        for item in entries:
            temp = item.get("temperature")
            when = dt_util.parse_datetime(item.get("datetime") or "")
            if temp is not None and when is not None:
                try:
                    parsed.append((when, float(temp)))
                except (TypeError, ValueError):
                    continue
        self._forecast = parsed

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
