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

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import logging
import math

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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONDOMINIO_PV_REMAINING,
    CONSENSO_BLOCCO,
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    COOL_GAIN_SOLAR,
    CLEAR_SKY_GHI,
    COOL_PULLDOWN,
    DEFAULT_PV_BIAS_COAST_RELAX,
    DEFAULT_PV_BIAS_DAILY_NEED_KWH,
    DEFAULT_PV_BIAS_EFF_FRACTION,
    DEFAULT_PV_BIAS_EFF_MIN,
    DEFAULT_PV_BIAS_FLOOR_POOR,
    DEFAULT_PV_BIAS_FLOOR_RICH,
    FORECASTSOLAR_GHI_FACTOR,
    FORECASTSOLAR_POWER,
    OPT_PV_BIAS_COAST_RELAX,
    OPT_PV_BIAS_DAILY_NEED_KWH,
    OPT_PV_BIAS_EFF_FRACTION,
    OPT_PV_BIAS_EFF_MIN,
    OPT_PV_BIAS_FLOOR_POOR,
    OPT_PV_BIAS_FLOOR_RICH,
    PV_BIAS_MIN_DWELL,
    COOL_VALVES,
    DEFAULT_COMFORT_DAY_FROM,
    DEFAULT_COMFORT_DAY_TO,
    DEFAULT_COMFORT_ENABLED,
    DEFAULT_COMFORT_NIGHT_FROM,
    DEFAULT_COMFORT_NIGHT_TO,
    DEFAULT_COMFORT_RELAX,
    OPT_COMFORT_DAY_FROM,
    OPT_COMFORT_DAY_TO,
    OPT_COMFORT_ENABLED,
    OPT_COMFORT_NIGHT_FROM,
    OPT_COMFORT_NIGHT_TO,
    OPT_COMFORT_RELAX,
    DEFAULT_BAND_SLAM,
    DEFAULT_BAND_WIDTH,
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_COOLOFF,
    DEFAULT_DUTY_MAX_STINT,
    DEFAULT_DUTY_PEAK_OUTDOOR,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_MIN_COMPRESSOR_OFF,
    DEFAULT_MIN_COMPRESSOR_ON,
    DEFAULT_MODEL_ENABLED,
    DEFAULT_PRECOOL_LOOKAHEAD_HOURS,
    DEFAULT_PRECOOL_MARGIN,
    DEFAULT_PRECOOL_MAX_DEPTH,
    DEFAULT_PRECOOL_OFFSET,
    DEFAULT_REGIME_ENABLED,
    DEFAULT_REGIME_MEDIUM_RATIO,
    DEFAULT_REGIME_PEAK_RATIO,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SOLAR_FORECAST,
    DEFAULT_SHADING_POSITION,
    DEFAULT_SHADING_SOLAR,
    FORECAST_REFRESH,
    OPT_BAND_SLAM,
    OPT_BAND_WIDTH,
    OPT_DUTY_COMFORT_MAX,
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_DUTY_PEAK_OUTDOOR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_MIN_COMPRESSOR_OFF,
    OPT_MIN_COMPRESSOR_ON,
    OPT_MODEL_ENABLED,
    OPT_PRECOOL_LOOKAHEAD_HOURS,
    OPT_PRECOOL_MARGIN,
    OPT_PRECOOL_MAX_DEPTH,
    OPT_PRECOOL_OFFSET,
    OPT_REGIME_ENABLED,
    OPT_REGIME_MEDIUM_RATIO,
    OPT_REGIME_PEAK_RATIO,
    OPT_SOLAR_FORECAST,
    PLAN_SIM_DOWNSAMPLE_MIN,
    PLAN_SIM_STEP_MIN,
    REGIME_K_CONF_MIN,
    SEASON_SUMMER,
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
    fan_min,
    fan_pacing_enabled,
    is_zone_disabled,
    mode_offset,
    pv_bias_enabled,
    shade_blocked,
    shade_position,
    supervisor_enabled,
)
from .policies import (
    DutyController,
    FanBandController,
    RegimeCoordinator,
    ThermalEstimator,
)
from .returnhome import AwayReturnController
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
    RoomParams,
    build_plan,
    build_room_plans,
    house_load_index,
    in_window,
    merge_desired,
    plan_run,
    reconcile,
    select_regime,
    solar_curve_v2,
    _forecast_temp_at,
    cooling_effectiveness,
    energy_precool_decision,
)

_LOGGER = logging.getLogger(__name__)

# Fail-safe waits at most this long for an in-flight cycle to finish before
# releasing anyway — a redundant write beats a block stranded behind a wedged
# cycle (the hand-back must never hang on the cooling-block release).
_FAILSAFE_LOCK_TIMEOUT = 5.0


def _num(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a sensor whose state is a number (None if missing/non-numeric).

    Rejects NaN/inf: a sensor reporting 'nan' would otherwise poison every
    comparison (NaN compares False, so at-peak / free-cool guards silently
    disable) and 'inf' would pin at-peak on forever."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        val = float(state.state)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _outdoor_temp(hass: HomeAssistant) -> float | None:
    """Ecowitt outdoor temp, falling back to the PdC's own probe."""
    val = _num(hass, OUTDOOR_TEMP)
    return val if val is not None else _num(hass, OUTDOOR_TEMP_FALLBACK)


def _manuale_switch(fan_entity: str) -> str:
    """Manuale switch for a fancoil fan (verified: fan.fancoil_X -> switch.fancoil_X_manuale)."""
    return "switch." + fan_entity.removeprefix("fan.") + "_manuale"


def _forecast_cloud_at(cloud: list, when) -> float | None:
    """The cloud fraction at/before `when` from the cached (when, cloud) list."""
    best = None
    for w, c in cloud:
        if w <= when:
            best = c
        else:
            break
    return best


def _parse_hhmm(value: str, fallback: int) -> int:
    """'HH:MM' -> minute-of-day; fallback on garbage."""
    try:
        h, m = str(value).split(":")[:2]
        return (int(h) % 24) * 60 + (int(m) % 60)
    except (ValueError, AttributeError):
        return fallback


@dataclass(frozen=True)
class _Comfort:
    """F4b resolved comfort schedule for this cycle (capped relax + windows)."""

    relax: float
    day: tuple[int, int]
    night: tuple[int, int]
    minute: int

    def relax_for(self, *, bedroom: bool) -> float:
        frm, to = self.night if bedroom else self.day
        return 0.0 if in_window(self.minute, frm, to) else self.relax


def _comfort_config(hass: HomeAssistant, entry: ConfigEntry, center: float | None) -> "_Comfort | None":
    """Build the comfort schedule, or None when disabled / no center. The relax is
    pre-capped so center+relax can never exceed duty_comfort_max."""
    if center is None or not entry.options.get(OPT_COMFORT_ENABLED, DEFAULT_COMFORT_ENABLED):
        return None
    comfort_max = float(entry.options.get(OPT_DUTY_COMFORT_MAX, DEFAULT_DUTY_COMFORT_MAX))
    relax = min(
        float(entry.options.get(OPT_COMFORT_RELAX, DEFAULT_COMFORT_RELAX)),
        max(0.0, comfort_max - center),
    )
    local = dt_util.now()
    return _Comfort(
        relax=relax,
        day=(
            _parse_hhmm(entry.options.get(OPT_COMFORT_DAY_FROM, DEFAULT_COMFORT_DAY_FROM), 480),
            _parse_hhmm(entry.options.get(OPT_COMFORT_DAY_TO, DEFAULT_COMFORT_DAY_TO), 1380),
        ),
        night=(
            _parse_hhmm(entry.options.get(OPT_COMFORT_NIGHT_FROM, DEFAULT_COMFORT_NIGHT_FROM), 1320),
            _parse_hhmm(entry.options.get(OPT_COMFORT_NIGHT_TO, DEFAULT_COMFORT_NIGHT_TO), 480),
        ),
        minute=local.hour * 60 + local.minute,
    )


class RoomModelStore:
    """Durable per-room learned thermal models (F2). The first Store the
    integration owns. Best-effort: load/save swallow errors so a corrupt or
    unwritable file can never block setup or the fail-safe."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, "villa_hvac_room_models")

    async def async_load(self) -> dict:
        try:
            return (await self._store.async_load()) or {}
        except Exception:  # noqa: BLE001 - corrupt store must fall back to priors
            _LOGGER.warning("Could not load room-model store; seeding priors", exc_info=True)
            return {}

    async def async_save(self, data: dict) -> None:
        await self._store.async_save(data)


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
    # F2: the learned thermal model (blended prior->learned) for each leader.
    thermal = getattr(getattr(coordinator, "engine", None), "thermal", None)

    # Hoisted once (reused for the F4b comfort relax + the HouseState below).
    mode = current_house_mode(hass, entry)
    house_setpoint = current_house_setpoint(hass, entry)
    house_offset = mode_offset(hass, entry, mode)
    center = (
        house_setpoint + house_offset
        if (house_setpoint is not None and house_offset is not None) else None
    )
    comfort = _comfort_config(hass, entry, center)  # None when disabled / no center

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
        manuale = zone.get("manuale_switch") or (
            _manuale_switch(fancoil) if fancoil else None
        )
        emitter = zone.get("emitter")
        # F2b: current commanded fan % + whether ANY unit's manuale is on (held).
        fan_pct: int | None = None
        manuale_on = False
        if emitter == "fancoil" and fancoils:
            fs = hass.states.get(fancoils[0])
            if fs is not None:
                try:
                    fan_pct = int(float(fs.attributes.get(ATTR_PERCENTAGE)))
                except (TypeError, ValueError):
                    fan_pct = None
            for f in fancoils:
                ms = hass.states.get(_manuale_switch(f))
                if ms is not None and ms.state == STATE_ON:
                    manuale_on = True
                    break
        # F2: blended thermal model for cooling-fancoil leaders (None otherwise).
        m_a = m_b = m_c = m_k = m_conf = m_kconf = None
        is_leader = bool(
            zone.get("climate") and emitter == "fancoil"
            and fancoils and not zone.get("follows")
        )
        if thermal is not None and is_leader:
            m = thermal.model_for(zone_id)
            abc_conf, k_conf = thermal.confidence(zone_id)
            m_a, m_b, m_c, m_k = m.a, m.b, m.c, m.k
            m_conf, m_kconf = min(abc_conf, k_conf), k_conf
        # F4b: relax the band center while OUTSIDE this room's comfort window.
        comfort_relax = (
            comfort.relax_for(bedroom=bool(zone.get("bedroom")))
            if (comfort is not None and is_leader) else 0.0
        )
        zones[zone_id] = ZoneSnapshot(
            zone_id=zone_id,
            name=zone["name"],
            climate=zone.get("climate"),
            emitter=emitter,
            temp=(zone_temps.get(zone_id) or {}).get("value"),
            demand=demand,
            enabled=not is_zone_disabled(hass, entry, zone_id),
            paused=zone_id in paused,
            bedroom=bool(zone.get("bedroom")),
            fan_min=fan_min(hass, entry, zone_id) if emitter == "fancoil" else 0,
            fancoil=fancoil,
            manuale=manuale,
            follows=zone.get("follows"),
            # every fancoil this leader drives, paired with its manuale switch
            # (living_room owns Salotto + Cucina) — #3 v2 capacity control.
            fancoil_units=tuple((f, _manuale_switch(f)) for f in fancoils),
            model_a=m_a, model_b=m_b, model_c=m_c, model_k=m_k,
            model_confidence=m_conf, model_k_confidence=m_kconf,
            fan_pct=fan_pct, manuale_on=manuale_on,
            comfort_relax=comfort_relax,
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
        band_width=float(entry.options.get(OPT_BAND_WIDTH, DEFAULT_BAND_WIDTH)),
        band_slam=float(entry.options.get(OPT_BAND_SLAM, DEFAULT_BAND_SLAM)),
        model_learning_enabled=bool(
            entry.options.get(OPT_MODEL_ENABLED, DEFAULT_MODEL_ENABLED)
        ),
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
        comfort_enabled=comfort is not None,
        season=current_season(hass, entry),
        house_mode=mode,
        auto_setback=auto_setback_enabled(hass, entry),
        house_setpoint=house_setpoint,
        mode_offset=house_offset,
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
        model_store: "RoomModelStore | None" = None,
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
        # F2: the online thermal-model OBSERVER. NOT a merge controller — it never
        # actuates; the engine ticks it every cycle (even deploy-dark) so passive
        # params converge before actuation lights up.
        self.thermal = ThermalEstimator()
        # F3c: the coalescing coordinator. Driven explicitly (needs regime+center);
        # emits a per-leader phase_override + a BLOCCO opinion. Placed BEFORE the
        # DutyController in the merge so its BLOCCO wins when it's coalescing.
        self.regime = RegimeCoordinator()
        # #8: overrides the effective house mode while Via+armed (deep setback ->
        # pre-cond ramp). Applied to the state before policies run. Holds the latch.
        self.away_return = AwayReturnController()
        self._fan_controller = next(
            (c for c in self.controllers if isinstance(c, FanBandController)), None
        )
        self._model_store = model_store
        self._model_saved_ts = None
        self._lever_states: dict[str, LeverState] = {}
        self._unsub = None
        # One lock serialises every cycle (a scheduled tick + an awaited
        # request_run can otherwise interleave over the shared lever/forecast/
        # controller state). `_stopped` short-circuits any pass that is scheduled
        # or in-flight across teardown, so a late task can't re-actuate after the
        # fail-safe has handed the villa back.
        self._lock = asyncio.Lock()
        self._stopped = False
        self._forecast: list[tuple] = []
        self._cloud: list[tuple] = []
        self._forecast_ts = None
        self._plan_view: PlanView | None = None
        # PV/energy-aware pre-cool: last decision (diagnostic) + min-dwell anchor.
        self._pv_decision = None
        self._pv_since = None

    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self._on_update)

    def stop(self) -> None:
        self._stopped = True
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
        # Skip if a cycle is already running (the lock serialises anyway; this
        # just avoids piling redundant ticks). Use a config-entry background task
        # so HA cancels it on unload — a late tick must not re-actuate after the
        # fail-safe.
        if self._stopped or self._lock.locked():
            return
        self.entry.async_create_background_task(
            self.hass, self._tick(), "villa_hvac_supervisor_tick"
        )

    async def request_run(self) -> None:
        """Run a supervisor pass immediately (event-driven responsiveness).

        Lets event sources — a window opening, a mode change, a zone toggle —
        get an instant pass instead of waiting for the 30 s tick, while the
        engine stays the single writer. No-op while disabled or stopped; if a
        cycle is in flight this queues on the lock rather than being dropped, so
        the event is always honoured (and never interleaves).
        """
        if self.enabled and not self._stopped:
            await self._run()

    async def _tick(self) -> None:
        """Periodic pass: refresh the plan always, actuate only when enabled."""
        await self._cycle(actuate=self.enabled)

    async def _run(self) -> None:
        """Force an actuating pass (event-driven + tests). Also refreshes plan."""
        await self._cycle(actuate=True)

    async def _cycle(self, *, actuate: bool) -> None:
        if self._stopped:
            return
        await self._lock.acquire()
        try:
            # Re-check after acquiring: the engine may have stopped while we were
            # queued behind another pass — a post-teardown cycle must not actuate.
            if self._stopped:
                return
            await self._maybe_refresh_forecast()
            state = build_house_state(
                self.hass, self.entry, self.coordinator, forecast=self._forecast
            )
            # F2: learn the per-room model EVERY cycle, even deploy-dark (the
            # observer never actuates; passive params converge before go-live).
            # Observe the RAW state (before the #8 mode override, which only
            # changes intended presets/setpoints, not the measured conditions).
            self.thermal.observe(state)
            await self._maybe_persist_model()
            # #8: override the effective house mode while Via+armed (deep setback
            # -> pre-cond ramp). Both the plan view and actuation see it; the latch
            # only advances on an actuating pass.
            state = self.away_return.apply(
                state, self.hass, self.entry, commit=actuate
            )
            # PV/energy-aware daily pre-cool (F4c-lite): sets pv_mode/floor on the
            # state so the band controller banks in efficient hours / defers in the
            # evening. Stateless (decides from forecasts) — safe for the plan view too.
            state = self._pv_bias_apply(state)
            # Pure policies first — used both for the plan view and (merged with
            # the stateful controllers) for actuation.
            pure_outputs = [policy(state) for policy in self.policies]
            self._plan_view = self._build_plan_view(state, pure_outputs)
            if actuate:
                # F3c: advance the coalescing coordinator first (it owns BLOCCO +
                # the per-leader phase_override when MEDIUM-coalescing). Its BLOCCO
                # opinion is merged BEFORE the DutyController so it wins; when it
                # yields, the legacy duty BLOCCO survives.
                phase_override, regime_blocco = self._regime_step(state)
                ctrl_outputs: list = []
                if regime_blocco is not None:
                    ctrl_outputs.append({BLOCCO_LEVER: regime_blocco})
                for c in self.controllers:
                    if c is self._fan_controller:
                        ctrl_outputs.append(c(state, phase_override=phase_override))
                    else:
                        ctrl_outputs.append(c(state))
                # Controllers first: the #3 band controller's setpoint must win
                # over house_mode for the cooling zones it actively manages. It
                # already yields (no opinion) on disabled/paused/free-cool zones,
                # so the higher-priority preset policies still own those.
                desired = merge_desired([*ctrl_outputs, *pure_outputs])
                for lever, target in desired.items():
                    # Bail the moment teardown begins (stop()) so a fail-safe
                    # hand-back is never chased by more lever writes mid-loop.
                    if self._stopped:
                        break
                    await self._reconcile_lever(lever, target, state)
        finally:
            self._lock.release()

    async def _maybe_persist_model(self) -> None:
        """Persist the learned models at most once per FORECAST_REFRESH; best-effort."""
        if self._model_store is None:
            return
        now = dt_util.utcnow()
        if self._model_saved_ts is not None and now - self._model_saved_ts < FORECAST_REFRESH:
            return
        self._model_saved_ts = now
        try:
            await self._model_store.async_save(self.thermal.dump())
        except Exception:  # noqa: BLE001 - persistence is best-effort, never fatal
            _LOGGER.debug("Room-model persist failed", exc_info=True)

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
        plan = build_plan(
            state, run_plan, desired, duty_state,
            list(self._forecast), _lookahead(self.entry),
        )
        # F3a: classify the house regime read-only (pure; deploy-dark safe). On
        # priors the ratio is mis-scaled, so it's trusted only for converged-k
        # zones; PEAK still keys off at_peak alone.
        load = house_load_index(
            state,
            default_a=COOL_GAIN_OUTDOOR, default_b=COOL_GAIN_SOLAR,
            default_c=COOL_GAIN_BASE, default_capacity=COOL_CAPACITY,
            k_conf_min=REGIME_K_CONF_MIN,
        )
        regime = select_regime(
            load, at_peak=plan.at_peak, free_cool=plan.free_cool,
            peak_ratio=float(
                self.entry.options.get(OPT_REGIME_PEAK_RATIO, DEFAULT_REGIME_PEAK_RATIO)
            ),
            medium_ratio=float(
                self.entry.options.get(
                    OPT_REGIME_MEDIUM_RATIO, DEFAULT_REGIME_MEDIUM_RATIO
                )
            ),
        )
        # F3b: per-room 12h forward simulation + pre-cool (pure, plan-only).
        solar_curve, solar_model = self._solar_forecast(state)
        trajectories = build_room_plans(
            state, self._room_params(state), list(self._forecast),
            solar_curve, _lookahead(self.entry),
            dt_min=PLAN_SIM_STEP_MIN, downsample_min=PLAN_SIM_DOWNSAMPLE_MIN,
            max_precool_depth=float(
                self.entry.options.get(OPT_PRECOOL_MAX_DEPTH, DEFAULT_PRECOOL_MAX_DEPTH)
            ),
        )
        return replace(
            plan, regime=regime, g_house=load.g_house, k_house=load.k_house,
            load_ratio=load.load_ratio, room_trajectories=trajectories,
            solar_model=solar_model,
        )

    def _regime_step(self, state: HouseState) -> tuple[dict[str, str], str | None]:
        """F3c: classify the regime and, if coalescing is enabled + MEDIUM, advance
        the coordinator and return (phase_override, BLOCCO opinion). Gated by
        regime_enabled AND duty_cycle AND fan_pacing; otherwise resets + yields."""
        coalescing = (
            self.entry.options.get(OPT_REGIME_ENABLED, DEFAULT_REGIME_ENABLED)
            and duty_cycle_enabled(self.hass, self.entry)
            and fan_pacing_enabled(self.hass, self.entry)
        )
        if not coalescing:
            return self.regime.step(state, regime="low", center=None, min_on=None, min_off=None)
        load = house_load_index(
            state, default_a=COOL_GAIN_OUTDOOR, default_b=COOL_GAIN_SOLAR,
            default_c=COOL_GAIN_BASE, default_capacity=COOL_CAPACITY,
            k_conf_min=REGIME_K_CONF_MIN,
        )
        at_peak = (
            state.outdoor_temp is not None and state.duty_peak_outdoor is not None
            and state.outdoor_temp >= state.duty_peak_outdoor
        )
        free_cool = (
            state.free_cool_enabled and state.season == SEASON_SUMMER
            and state.outdoor_temp is not None and state.free_cool_threshold is not None
            and state.outdoor_temp < state.free_cool_threshold
        )
        regime = select_regime(
            load, at_peak=at_peak, free_cool=free_cool,
            peak_ratio=float(
                self.entry.options.get(OPT_REGIME_PEAK_RATIO, DEFAULT_REGIME_PEAK_RATIO)
            ),
            medium_ratio=float(
                self.entry.options.get(OPT_REGIME_MEDIUM_RATIO, DEFAULT_REGIME_MEDIUM_RATIO)
            ),
        )
        center = (
            state.house_setpoint + state.mode_offset
            if (state.house_setpoint is not None and state.mode_offset is not None)
            else None
        )
        return self.regime.step(
            state, regime=regime, center=center,
            min_on=timedelta(
                minutes=float(self.entry.options.get(OPT_MIN_COMPRESSOR_ON, DEFAULT_MIN_COMPRESSOR_ON))
            ),
            min_off=timedelta(
                minutes=float(self.entry.options.get(OPT_MIN_COMPRESSOR_OFF, DEFAULT_MIN_COMPRESSOR_OFF))
            ),
        )

    def _room_params(self, state: HouseState) -> dict[str, RoomParams]:
        """Per-leader RoomParams from the blended model on each ZoneSnapshot (prior
        until a room converges) — the same model the controller uses."""
        out: dict[str, RoomParams] = {}
        for z in state.zones.values():
            if not (z.climate and z.emitter == "fancoil" and z.fancoil_units and not z.follows):
                continue
            out[z.zone_id] = RoomParams(
                a=z.model_a if z.model_a is not None else COOL_GAIN_OUTDOOR,
                b=z.model_b if z.model_b is not None else COOL_GAIN_SOLAR,
                c=z.model_c if z.model_c is not None else COOL_GAIN_BASE,
                k=z.model_k if (z.model_k and z.model_k > 0) else COOL_CAPACITY,
                pulldown=COOL_PULLDOWN, fan_min=z.fan_min,
            )
        return out

    def _nowcast_actual(self, state: HouseState) -> float | None:
        """Live GHI anchor for the solar curve: the gw3000a pyranometer, else the
        Forecast.Solar PV power scaled to a GHI-equivalent (fallback only)."""
        if state.solar is not None:
            return state.solar
        pv = _num(self.hass, FORECASTSOLAR_POWER)
        return pv * FORECASTSOLAR_GHI_FACTOR if pv is not None else None

    def _solar_forecast(self, state: HouseState) -> tuple[list[float], str]:
        """Per-step solar estimate over the lookahead + a model marker.

        F4a-v2 (opt-in OPT_SOLAR_FORECAST): sun elevation (astral) × clear-sky ×
        forecast cloud, then NOWCAST-ANCHORED to the live gw3000a (the regional
        cloud is unreliable here). Marker: `nowcast` when anchored, `forecast`
        when only the shape was available, `flat` (current solar) when disabled."""
        n = int(_lookahead(self.entry).total_seconds() / 60 // PLAN_SIM_STEP_MIN) + 1
        if not self.entry.options.get(OPT_SOLAR_FORECAST, DEFAULT_SOLAR_FORECAST):
            cur = state.solar if state.solar is not None else 0.0
            return [cur] * n, "flat"
        try:
            from astral import Observer
            from astral.sun import elevation as _sun_elevation

            obs = Observer(
                latitude=self.hass.config.latitude,
                longitude=self.hass.config.longitude,
                elevation=self.hass.config.elevation or 0.0,
            )
            elevations: list[float] = []
            clouds: list[float | None] = []
            for i in range(n):
                when = state.now + timedelta(minutes=i * PLAN_SIM_STEP_MIN)
                elevations.append(_sun_elevation(obs, when))
                clouds.append(_forecast_cloud_at(self._cloud, when))
            curve, anchored = solar_curve_v2(
                elevations=elevations, clouds=clouds, clear_sky_ghi=CLEAR_SKY_GHI,
                actual_now=self._nowcast_actual(state),
            )
            return curve, ("nowcast" if anchored else "forecast")
        except Exception:  # noqa: BLE001 - solar estimate is best-effort -> fall back
            _LOGGER.debug("Solar forecast failed; using flat prior", exc_info=True)
            cur = state.solar if state.solar is not None else 0.0
            return [cur] * n, "flat"

    def _house_cooling_model(self, state: HouseState) -> tuple[float, float, float, float]:
        """Mean (a, b, c, k) over the cooling leader zones (blended model else priors)
        — the aggregate the PV effectiveness ranking reasons over."""
        params = self._room_params(state)
        if not params:
            return COOL_GAIN_OUTDOOR, COOL_GAIN_SOLAR, COOL_GAIN_BASE, COOL_CAPACITY
        n = len(params)
        return (
            sum(p.a for p in params.values()) / n,
            sum(p.b for p in params.values()) / n,
            sum(p.c for p in params.values()) / n,
            sum(p.k for p in params.values()) / n,
        )

    def _pv_bias_apply(self, state: HouseState) -> HouseState:
        """PV/energy-aware daily pre-cool (F4c-lite). Rank the next lookahead hours by
        cooling effectiveness (model × temp/solar forecast), overlay the daily
        solar-vs-consumption balance, and set pv_mode/floor so the band controller
        banks coolth in the efficient hours and defers in the hot evening. Works ONLY
        through the band center → requires fan_pacing; summer only; no BLOCCO/duty
        coupling. Best-effort: any failure yields no PV opinion.

        NOTE: keep pv_bias and regime coalescing (OPT_REGIME_ENABLED) from being on
        together for now — the coordinator decides RUN/REST off the BASE center while
        the band applies the PV-shifted one, so their composition is unverified."""
        self._pv_decision = None
        # center = the real band center (setpoint + mode_offset); the effectiveness
        # must be evaluated there, not at the bare setpoint. mode_offset is None in
        # Vacanza / return-waiting -> yield (cooling is building_protection anyway).
        if not (
            pv_bias_enabled(self.hass, self.entry)
            and fan_pacing_enabled(self.hass, self.entry)
            and state.season == SEASON_SUMMER
            and state.house_setpoint is not None
            and state.mode_offset is not None
        ):
            self._pv_since = None
            return state
        try:
            # The effectiveness ranking's solar dimension needs a REAL forecast curve;
            # the flat prior (OPT_SOLAR_FORECAST off) collapses the peak-defer shape.
            solar_curve, solar_marker = self._solar_forecast(state)
            if not solar_curve or solar_marker == "flat":
                self._pv_since = None
                return state
            a, b, c, k = self._house_cooling_model(state)
            center = state.house_setpoint + state.mode_offset
            eff: list[float] = []
            for i, s in enumerate(solar_curve):
                when = state.now + timedelta(minutes=i * PLAN_SIM_STEP_MIN)
                t_out = _forecast_temp_at(self._forecast, when)
                if t_out is None:
                    t_out = state.outdoor_temp
                eff.append(cooling_effectiveness(center, t_out, s, a=a, b=b, c=c, k=k))
            pv_kwh = _num(self.hass, CONDOMINIO_PV_REMAINING)
            local = dt_util.now()  # LOCAL day clock (state.now is UTC)
            frac_remaining = max(0.0, (1440 - (local.hour * 60 + local.minute)) / 1440.0)
            daily_need = float(
                self.entry.options.get(
                    OPT_PV_BIAS_DAILY_NEED_KWH, DEFAULT_PV_BIAS_DAILY_NEED_KWH
                )
            )
            raw = energy_precool_decision(
                effectiveness=eff, now_index=0,
                pv_kwh_remaining=pv_kwh,
                consumption_kwh_remaining=daily_need * frac_remaining,
                eff_fraction=float(
                    self.entry.options.get(
                        OPT_PV_BIAS_EFF_FRACTION, DEFAULT_PV_BIAS_EFF_FRACTION
                    )
                ),
                eff_min=float(
                    self.entry.options.get(OPT_PV_BIAS_EFF_MIN, DEFAULT_PV_BIAS_EFF_MIN)
                ),
                floor_rich=float(
                    self.entry.options.get(
                        OPT_PV_BIAS_FLOOR_RICH, DEFAULT_PV_BIAS_FLOOR_RICH
                    )
                ),
                floor_poor=float(
                    self.entry.options.get(
                        OPT_PV_BIAS_FLOOR_POOR, DEFAULT_PV_BIAS_FLOOR_POOR
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - PV bias is best-effort, never break the cycle
            _LOGGER.debug("PV bias failed; no opinion this cycle", exc_info=True)
            self._pv_since = None
            return state
        # Min-dwell anti-thrash: hold the previous decision when the mode would flip
        # before PV_BIAS_MIN_DWELL elapses (a mode flip jumps the center ~2°C >> band,
        # which would slam the valve). state.now is UTC — fine for a duration.
        prev = self._pv_decision if self._pv_since is not None else None
        if (
            prev is not None and prev.mode != raw.mode
            and state.now - self._pv_since < PV_BIAS_MIN_DWELL
        ):
            decision = prev
        else:
            decision = raw
            if self._pv_since is None or (prev is not None and prev.mode != raw.mode):
                self._pv_since = state.now
        self._pv_decision = decision
        coast_relax = float(
            self.entry.options.get(
                OPT_PV_BIAS_COAST_RELAX, DEFAULT_PV_BIAS_COAST_RELAX
            )
        )
        return replace(
            state, pv_mode=decision.mode, pv_floor=decision.floor,
            pv_coast_relax=coast_relax,
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
            # The global cooling block must always be releasable/assertable by the
            # engine: never concede it to a phantom "manual" change from bus noise.
            allow_override=lever != BLOCCO_LEVER,
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
        clouds: list[tuple] = []
        for item in entries:
            temp = item.get("temperature")
            when = dt_util.parse_datetime(item.get("datetime") or "")
            if temp is not None and when is not None:
                try:
                    parsed.append((when, float(temp)))
                except (TypeError, ValueError):
                    continue
                # F4a: capture cloud cover (0-1) for the solar estimate; often absent.
                cc = item.get("cloud_coverage")
                clouds.append((when, float(cc) / 100.0 if cc is not None else None))
        self._forecast = parsed
        self._cloud = clouds

    # -- fail-safe ------------------------------------------------------------
    async def _release_blocco(self) -> None:
        """Raw UNCONDITIONAL release of the central cooling block. The caller
        holds self._lock. Never gates on the read — a transient
        `unavailable`/`unknown` KNX value (the lossy-bus ambiguity `reconcile`
        distrusts) must not skip the release while the object is physically
        blocked. Turning an already-off switch off is a harmless no-op.
        """
        try:
            await self._call_switch(CONSENSO_BLOCCO, on=False)
        except Exception:  # noqa: BLE001 - the safety release must never raise
            _LOGGER.exception("Could not release %s", CONSENSO_BLOCCO)

    async def async_release_blocco(self) -> None:
        """Boot / external safe baseline: release the block, SERIALIZED against
        cycles (so an in-flight cycle can't re-block after us). Skips only when
        the KNX entity isn't present yet — a cold boot before its integration
        loaded; nothing to target, and _startup_resync re-runs this once it is.
        """
        if self.hass.states.get(CONSENSO_BLOCCO) is None:
            return
        async with self._lock:
            await self._release_blocco()

    async def async_fail_safe(self) -> None:
        """Hand the villa back to native KNX: release the central cooling block
        AND release every fancoil from MANUAL (fans → AUTO), unconditionally.

        Invariant: the villa must never stay globally blocked, nor with a fan
        pinned in manual, without the supervisor alive.

        Serialized against an in-flight cycle so its lever writes can never land
        AFTER our release — but bounded, so a wedged cycle can never make the
        hand-back hang: after `_FAILSAFE_LOCK_TIMEOUT` we release regardless.
        """
        locked = False
        try:
            await asyncio.wait_for(
                self._lock.acquire(), timeout=_FAILSAFE_LOCK_TIMEOUT
            )
            locked = True
        except TimeoutError:
            _LOGGER.warning("Fail-safe: cycle lock busy; releasing without it")
        try:
            await self._release_blocco()
            # Release every fancoil manuale UNCONDITIONALLY too (fans -> AUTO):
            # same fail-open rationale as the block — a stale read must not leave
            # a fan pinned in manual with the supervisor gone.
            for zone in ZONES.values():
                for fan in zone.get("fancoils") or []:
                    manuale = _manuale_switch(fan)
                    try:
                        await self._call_switch(manuale, on=False)
                    except Exception:  # noqa: BLE001 - fail-safe must not raise
                        _LOGGER.exception("Fail-safe: could not release %s", manuale)
        finally:
            if locked:
                self._lock.release()
        # F2: persist the learned models LAST (after lever release), best-effort —
        # a corrupt/half-written file must never block the BLOCCO/manuale release.
        if self._model_store is not None:
            try:
                await self._model_store.async_save(self.thermal.dump())
            except Exception:  # noqa: BLE001 - persistence must not break fail-safe
                _LOGGER.debug("Fail-safe: room-model persist failed", exc_info=True)
