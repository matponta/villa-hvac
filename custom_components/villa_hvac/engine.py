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
    STATE_OPEN,
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
    COOL_PULLDOWN_HOURS,
    COOL_RUN_FAN_FLOOR,
    FORECASTSOLAR_GHI_FACTOR,
    FORECASTSOLAR_POWER,
    PV_BIAS_MIN_DWELL,
    COOL_VALVES,
    FORECAST_REFRESH,
    HOUSE_MODE_NIGHT,
    PLAN_SIM_DOWNSAMPLE_MIN,
    PLAN_SIM_STEP_MIN,
    REGIME_K_CONF_MIN,
    SCHEDULE_MAX_AGE,
    SEASON_SUMMER,
    OPT_WEATHER_ENTITY,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    PRESET_AUTO,
    PRESET_BUILDING_PROTECTION,
    PRESET_CONTROLLABLE_EMITTERS,
    RUN_FAN_OFF_WARN_CYCLES,
    SHADE_POSITION_TOLERANCE,
    SHADING_ORIENTATIONS,
    SHADING_SKIP_AREAS,
    SOLAR_RADIATION,
    STALE_TEMP_CYCLES,
    WEATHER_ENTITY_DEFAULT,
    ZONES,
)
from .controller import (
    auto_setback_enabled,
    comfort_floor,
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
    unified_planner_enabled,
)
from .policies import (
    CoolingController,
    ThermalEstimator,
)
from .returnhome import AwayReturnController
from .supervisor_config import SupervisorConfig
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
    _is_cooling_leader,
    _is_free_cooling,
    annotate_centers,
    build_plan,
    build_room_plans,
    house_load_index,
    in_window,
    merge_desired,
    plan_center_schedule,
    plan_run,
    reconcile,
    select_regime,
    solar_curve_v2,
    _forecast_temp_at,
    cooling_effectiveness,
    energy_precool_decision,
    SEFF_SOURCE_GHI,
    SEFF_UNITS_GHI,
    units_tag,
    zone_apertures,
    zone_effective_solar,
)

_LOGGER = logging.getLogger(__name__)

# Astral ships with HA core; hoisted to module top so the solar forecast doesn't
# re-import every solar-enabled cycle. Guarded so a (theoretical) missing astral
# degrades to the flat-solar fallback instead of breaking integration setup.
try:
    from astral import Observer as _AstralObserver
    from astral.sun import elevation as _sun_elevation
except ImportError:  # pragma: no cover - astral is a hard HA-core dependency
    _AstralObserver = None
    _sun_elevation = None

# Fail-safe waits at most this long for an in-flight cycle to finish before
# releasing anyway — a redundant write beats a block stranded behind a wedged
# cycle (the hand-back must never hang on the cooling-block release).
_FAILSAFE_LOCK_TIMEOUT = 5.0

# C5: bound every lever service call so one wedged KNX write can't stall the whole
# cycle (a KNX write normally lands sub-second; on timeout we log + move on and the
# reconcile re-asserts next cycle). Kept above _FAILSAFE_LOCK_TIMEOUT so the
# fail-safe still pre-empts a hang by releasing without the lock.
LEVER_CALL_TIMEOUT = 10.0


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


def _attr_float(state, attr: str) -> float | None:
    """A state attribute coerced to float, None on anything non-numeric — the
    S_eff geometry math must degrade to the "fallback" source on a garbage
    sun.sun attribute, never crash the cycle (isfinite-ingest convention)."""
    if state is None:
        return None
    try:
        return float(state.attributes.get(attr))
    except (TypeError, ValueError):
        return None


def _cover_position(hass: HomeAssistant, entity_id: str) -> int | None:
    """Live cover position (0 = down, 100 = open); closed/open states map to
    0/100 for covers without position support; None when unknown. Mirrors the
    engine's cover-lever read so the shading policy's never-raise min() and the
    reconcile compare the same value."""
    s = hass.states.get(entity_id)
    if s is None:
        return None
    pos = s.attributes.get(ATTR_CURRENT_POSITION)
    if pos is not None:
        try:
            return int(float(pos))
        except (TypeError, ValueError):
            return None
    if s.state == STATE_CLOSED:
        return 0
    if s.state == STATE_OPEN:
        return 100
    return None


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


def _comfort_config(cfg: SupervisorConfig, center: float | None) -> "_Comfort | None":
    """Build the comfort schedule, or None when disabled / no center. The relax is
    pre-capped so center+relax can never exceed duty_comfort_max."""
    if center is None or not cfg.comfort_enabled:
        return None
    relax = min(cfg.comfort_relax, max(0.0, cfg.duty_comfort_max - center))
    local = dt_util.now()
    return _Comfort(
        relax=relax,
        day=(
            _parse_hhmm(cfg.comfort_day_from, 480),
            _parse_hhmm(cfg.comfort_day_to, 1380),
        ),
        night=(
            _parse_hhmm(cfg.comfort_night_from, 1320),
            _parse_hhmm(cfg.comfort_night_to, 480),
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


def _make_run_plan(
    cfg: SupervisorConfig, forecast, now, outdoor: float | None
) -> RunPlan:
    """Build the #9 forecast run-plan from the cached forecast + parsed config.

    Shared by `build_house_state` (which keeps only `.precool`) and the #11 plan
    view (which surfaces the full plan), so both reason over the same horizon.
    """
    return plan_run(
        list(forecast),
        now,
        outdoor,
        peak_threshold=cfg.duty_peak_outdoor,
        lookahead=cfg.lookahead,
        margin=cfg.precool_margin,
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
    hass: HomeAssistant, entry: ConfigEntry, coordinator, forecast=(),
    base_covers: "tuple[CoverInfo, ...] | None" = None,
) -> HouseState:
    """Assemble one unified snapshot of the house for the policy stack.

    `forecast` is the cached hourly forecast (list of (datetime, temp)); the
    engine refreshes it out-of-band and passes it in so this stays sync + pure.
    `base_covers` is the cover set resolved from the registries — the engine
    caches it (invalidated on a registry update) to avoid a full-registry scan
    every cycle; None resolves it inline (used by tests).
    """
    data = coordinator.data or {}
    zone_temps = data.get("zone_temps") or {}
    window = getattr(coordinator, "window", None)
    paused = window.paused if window is not None else set()
    # F2: the learned thermal model (blended prior->learned) for each leader.
    thermal = getattr(getattr(coordinator, "engine", None), "thermal", None)
    # C3: parse + clamp every option ONCE for this cycle (kills the scattered
    # float(entry.options.get(...)) sites below); the clean config half the planner
    # reads, stored on the HouseState.
    cfg = SupervisorConfig.from_options(entry.options)

    # Hoisted once (reused for the F4b comfort relax + the HouseState below).
    mode = current_house_mode(hass, entry)
    house_setpoint = current_house_setpoint(hass, entry)
    house_offset = mode_offset(hass, entry, mode)
    center = (
        house_setpoint + house_offset
        if (house_setpoint is not None and house_offset is not None) else None
    )
    comfort = _comfort_config(cfg, center)  # None when disabled / no center

    # #6: enrich each shadeable cover with its room's shade target + block flag
    # + the live position (the shading policy's never-raise min() reads it).
    # Hoisted above the zone loop: the S_eff feed needs the enriched positions.
    resolved_covers = base_covers if base_covers is not None else shadeable_covers(hass)
    covers = tuple(
        replace(
            c,
            target_position=shade_position(hass, entry, c.zone) if c.zone else None,
            blocked=shade_blocked(hass, entry, c.zone) if c.zone else False,
            current_position=_cover_position(hass, c.entity_id),
        )
        for c in resolved_covers
    )
    sun = hass.states.get("sun.sun")
    sun_az = _attr_float(sun, "azimuth")
    sun_el = _attr_float(sun, "elevation")
    ghi = _num(hass, SOLAR_RADIATION)
    apertures_by_zone = zone_apertures(covers)
    # S_eff diagnostics: the would-be facade values are computed for every
    # leader EVERY cycle (deploy-dark style — visible on the model sensor
    # before the flag lights consumers up); the SNAPSHOT carries them only when
    # the flag is on, else the GHI identity (byte-identical to the GHI era).
    last_s_eff: dict[str, tuple[float | None, str, str]] = {}

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
                # An OFF fan delivers 0% regardless of the retained % attribute
                # (same blindness as the engine's fan-lever read): feeding the
                # retained % into the F2 capacity learner would credit cooling
                # effort that never happened. unavailable/unknown stays None.
                if fs.state == STATE_OFF:
                    fan_pct = 0
                elif fs.state == STATE_ON:
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
        m_eligible = False
        is_leader = bool(
            zone.get("climate") and emitter == "fancoil"
            and fancoils and not zone.get("follows")
        )
        if thermal is not None and is_leader:
            m = thermal.model_for(zone_id)
            abc_conf, k_conf = thermal.confidence(zone_id)
            m_a, m_b, m_c, m_k = m.a, m.b, m.c, m.k
            m_conf, m_kconf = min(abc_conf, k_conf), k_conf
            m_eligible = thermal.planner_eligible(zone_id)  # D1: planner gate
        # F4b: relax the band center while OUTSIDE this room's comfort window.
        comfort_relax = (
            comfort.relax_for(bedroom=bool(zone.get("bedroom")))
            if (comfort is not None and is_leader) else 0.0
        )
        # S_eff (STORY_SEFF): compute the facade value for every leader
        # (diagnostics), feed the snapshot only when the flag is on.
        z_s_eff: float | None = ghi
        z_src = SEFF_SOURCE_GHI
        z_units = SEFF_UNITS_GHI
        if is_leader:
            aps = apertures_by_zone.get(zone_id, ())
            facade_val, facade_src = zone_effective_solar(ghi, sun_el, sun_az, aps)
            facade_units = units_tag(aps)
            last_s_eff[zone_id] = (facade_val, facade_src, facade_units)
            if cfg.seff_enabled:
                z_s_eff, z_src, z_units = facade_val, facade_src, facade_units
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
            model_planner_eligible=m_eligible,
            fan_pct=fan_pct, manuale_on=manuale_on,
            comfort_relax=comfort_relax,
            s_eff=z_s_eff, s_eff_source=z_src, s_eff_units=z_units,
        )

    # Publish the per-leader diagnostic S_eff (model sensor reads it).
    eng = getattr(coordinator, "engine", None)
    if eng is not None:
        eng.last_s_eff = last_s_eff

    blocco_state = hass.states.get(CONSENSO_BLOCCO)
    # #2b night_active is DERIVED (C1): Notte + Auto-setback on + not auto-woken.
    # Computed here (not from a NightController.active flag) so it's consistent
    # with what NightSilenceController computes THIS cycle (no transition lag) and
    # so a reboot-in-Notte re-silences with no startup-resync branch.
    night = getattr(coordinator, "night", None)
    setback_on = auto_setback_enabled(hass, entry)
    night_active = (
        mode == HOUSE_MODE_NIGHT and setback_on and not getattr(night, "woken", False)
    )
    now = dt_util.utcnow()
    outdoor = _outdoor_temp(hass)
    plan = _make_run_plan(cfg, forecast, now, outdoor)
    return HouseState(
        now=now,
        zones=zones,
        covers=covers,
        sun_azimuth=sun_az,
        sun_elevation=sun_el,
        shading_enabled=cfg.shading_enabled,
        shading_solar_threshold=cfg.shading_solar,
        shading_default_position=cfg.shading_default_position,
        shading_proportional=cfg.shading_proportional,
        band_width=cfg.band_width,
        band_slam=cfg.band_slam,
        model_learning_enabled=cfg.model_learning_enabled,
        duty_enabled=duty_cycle_enabled(hass, entry),
        duty_max_stint=cfg.duty_max_stint,
        duty_cooloff=cfg.duty_cooloff,
        duty_comfort_max=cfg.duty_comfort_max,
        comfort_floor=comfort_floor(hass, entry, house_setpoint),
        duty_peak_outdoor=cfg.duty_peak_outdoor,
        precool=plan.precool,
        precool_offset=cfg.precool_offset,
        night_active=night_active,
        fan_pacing_enabled=fan_pacing_enabled(hass, entry),
        comfort_enabled=comfort is not None,
        season=current_season(hass, entry),
        house_mode=mode,
        auto_setback=setback_on,
        house_setpoint=house_setpoint,
        mode_offset=house_offset,
        free_cool_enabled=cfg.free_cool_enabled,
        free_cool_threshold=cfg.free_cool_outdoor,
        outdoor_temp=outdoor,
        solar=ghi,
        config=cfg,
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
        # Tier-1 M1: the ONE cooling organism (regime coalescing + duty BLOCCO +
        # band slam/fan, folded). The #11 plan view reads its .duty read-only.
        self._cooling = next(
            (c for c in self.controllers if isinstance(c, CoolingController)), None
        )
        # F2: the online thermal-model OBSERVER. NOT a merge controller — it never
        # actuates; the engine ticks it every cycle (even deploy-dark) so passive
        # params converge before actuation lights up.
        self.thermal = ThermalEstimator()
        # S_eff diagnostics: per-leader (value, source, units_tag) computed every
        # build_house_state (deploy-dark style) — the model sensor exposes it so
        # the geometry can be validated live before any consumer switches.
        self.last_s_eff: dict[str, tuple[float | None, str, str]] = {}
        # #8: overrides the effective house mode while Via+armed (deep setback ->
        # pre-cond ramp). Applied to the state before policies run. Holds the latch.
        self.away_return = AwayReturnController()
        self._model_store = model_store
        self._model_saved_ts = None
        self._lever_states: dict[str, LeverState] = {}
        # B2: last reconcile decision per lever this cycle (diagnostic only, surfaced
        # by sensor.hvac_levers) — the reconcile `note` + desired/current/attempts,
        # computed then discarded before this. Rebuilt each actuating cycle.
        self._lever_decisions: dict[str, dict] = {}
        self._unsub = None
        # C5: shadeable covers resolved once from the registries and cached, so we
        # don't scan the entire entity registry every 30 s cycle. Invalidated on a
        # registry-updated event (the resolution keys off entity/device labels +
        # area/floor, which only change via the registries).
        self._covers_cache: tuple[CoverInfo, ...] | None = None
        self._reg_unsubs: list = []
        # B4 diagnostic: consecutive cycles a controlled cooling leader has had no
        # fused temperature (it silently drops out of band control).
        self._stale_temp: dict[str, int] = {}
        # Watchdog: consecutive actuating cycles a fan lever is commanded >0%
        # while the fan reads OFF (delivered 0) — the KNX interlock keeps that
        # zone's valve closed, so it is not cooling despite a RUN command.
        self._fan_off: dict[str, int] = {}
        # R1 loud fallback: leaders already warned about reaching an actuating
        # pass with no resolved band center (annotate_centers lost/misordered).
        self._unresolved_center: set[str] = set()
        # One lock serialises every cycle (a scheduled tick + an awaited
        # request_run can otherwise interleave over the shared lever/forecast/
        # controller state). `_stopped` short-circuits any pass that is scheduled
        # or in-flight across teardown, so a late task can't re-actuate after the
        # fail-safe has handed the villa back.
        self._lock = asyncio.Lock()
        self._stopped = False
        # A hand-back (`async_fail_safe`) bumps `_epoch`; a cycle captures the epoch
        # before it queues on the lock and aborts if it changed while waiting. This
        # closes the re-slam window that `_stopped` alone misses on the master-OFF
        # path (engine stays alive, so `_stopped` is never set — but a cycle queued
        # with actuate=True could still re-block/re-slam AFTER the release, and with
        # the master now off nothing would ever clear it → a stranded block).
        self._epoch = 0
        self._forecast: list[tuple] = []
        self._cloud: list[tuple] = []
        self._forecast_ts = None
        self._plan_view: PlanView | None = None
        # PV/energy-aware pre-cool: last decision (diagnostic) + min-dwell anchor.
        self._pv_decision = None
        self._pv_since = None
        # F4c Phase 6: the cached unified band-center reference schedule. Recomputed
        # at the forecast cadence (or on a mode change), read forward by the fast
        # loop — a SLOW-moving reference, NOT recomputed every 30 s tick.
        self._center_schedule_cache = None
        self._schedule_ts = None
        self._schedule_mode = None

    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self._on_update)
        # Invalidate the shadeable-cover cache whenever the entity / device / area
        # registries change (a cover's area, orientation label or floor moving).
        self._reg_unsubs = [
            self.hass.bus.async_listen(event, self._invalidate_covers)
            for event in (
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                dr.EVENT_DEVICE_REGISTRY_UPDATED,
                ar.EVENT_AREA_REGISTRY_UPDATED,
            )
        ]

    def stop(self) -> None:
        self._stopped = True
        if self._unsub:
            self._unsub()
            self._unsub = None
        for unsub in self._reg_unsubs:
            unsub()
        self._reg_unsubs = []

    @callback
    def _invalidate_covers(self, _event=None) -> None:
        self._covers_cache = None

    def _resolve_covers(self) -> tuple[CoverInfo, ...]:
        """Cached shadeable-cover resolution (see `_invalidate_covers`)."""
        if self._covers_cache is None:
            self._covers_cache = shadeable_covers(self.hass)
        return self._covers_cache

    # -- enable gate ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True only when the master switch is explicitly on (deploy-dark)."""
        return supervisor_enabled(self.hass, self.entry)

    @property
    def plan_view(self) -> PlanView | None:
        """The latest #11 plan projection (refreshed each cycle, even dark)."""
        return self._plan_view

    @property
    def lever_decisions(self) -> dict[str, dict]:
        """B2: the last reconcile decision per lever this actuating cycle (empty
        while deploy-dark, since nothing actuates). Surfaced by sensor.hvac_levers."""
        return self._lever_decisions

    @property
    def stale_temp_leaders(self) -> list[str]:
        """B4: cooling leaders whose fused temp has been None for >= STALE_TEMP_CYCLES
        cycles (silently out of band control). Surfaced on sensor.hvac_plan."""
        return sorted(
            zid for zid, n in self._stale_temp.items() if n >= STALE_TEMP_CYCLES
        )

    def _track_stale_temp(self, state: HouseState) -> None:
        """B4 diagnostic: count consecutive cycles a CONTROLLED cooling leader has no
        fused temp, and WARN once when it crosses STALE_TEMP_CYCLES. A temp-less
        leader silently drops out of band control (band_step yields with no center),
        which is otherwise invisible. Runs every cycle, incl. deploy-dark."""
        for z in state.zones.values():
            controlled = (
                _is_cooling_leader(z) and z.enabled and not z.paused
                and not (z.bedroom and state.night_active)
            )
            if not controlled or z.temp is not None:
                self._stale_temp.pop(z.zone_id, None)
                continue
            n = self._stale_temp.get(z.zone_id, 0) + 1
            self._stale_temp[z.zone_id] = n
            if n == STALE_TEMP_CYCLES:  # log once, on the crossing
                _LOGGER.warning(
                    "Cooling leader %s has had no fused temperature for %d cycles — "
                    "it is not under band control until a reading returns",
                    z.zone_id, n,
                )

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
        epoch = self._epoch  # captured before we queue on the lock
        await self._lock.acquire()
        try:
            # Re-check after acquiring: the engine may have stopped, OR a fail-safe
            # hand-back may have run, while we were queued behind another pass — a
            # post-teardown / post-hand-back cycle must not actuate (else it could
            # re-block BLOCCO after the release; on master-OFF nothing would clear it).
            if self._stopped or epoch != self._epoch:
                return
            await self._maybe_refresh_forecast()
            state = build_house_state(
                self.hass, self.entry, self.coordinator,
                forecast=self._forecast, base_covers=self._resolve_covers(),
            )
            self._track_stale_temp(state)
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
            # F4c Phase 6: attach the cached unified reference schedule + the
            # planner-drive gate to the state, so BOTH the plan view and the
            # FanBandController see the same slow-moving reference.
            state = self._maybe_refresh_schedule(state)
            # R1: resolve the ONE per-leader band center onto the snapshots.
            # ORDER IS LOAD-BEARING — this must run AFTER the #8 mode override
            # (which rewrites house_mode/mode_offset and therefore the base
            # center), AFTER _pv_bias_apply (which attaches pv_mode/pv_floor),
            # and AFTER the schedule attach above. A wrong slot silently loses
            # PV/planner/precool in the resolution (no failure, just a warmer
            # house) — pinned by tests/test_resolve_center.py.
            state = annotate_centers(state, max_age=SCHEDULE_MAX_AGE)
            # Pure policies first — used both for the plan view and (merged with
            # the stateful controllers) for actuation.
            pure_outputs = [policy(state) for policy in self.policies]
            self._plan_view = self._build_plan_view(state, pure_outputs)
            if actuate:
                self._check_unresolved_centers(state)
                # Tier-1 M1: the regime/duty/band composition (BLOCCO precedence,
                # phase_override handoff, duty-always-advances) now lives INSIDE
                # CoolingController.__call__ — no more load-bearing list-ordering
                # here. Controllers first: the #3 band setpoint must win over
                # house_mode for the cooling zones it actively manages; it already
                # yields (no opinion) on disabled/paused/free-cool zones, so the
                # higher-priority preset policies still own those.
                ctrl_outputs = [c(state) for c in self.controllers]
                desired = merge_desired([*ctrl_outputs, *pure_outputs])
                for lever, target in desired.items():
                    # Bail the moment teardown begins (stop()) OR a fail-safe
                    # hand-back invalidates this cycle mid-loop (epoch bump): a
                    # wedged KNX write (LEVER_CALL_TIMEOUT 10 s) can outlive the
                    # fail-safe's bounded lock wait (_FAILSAFE_LOCK_TIMEOUT 5 s),
                    # and the resuming loop must never chase the hand-back with
                    # more lever writes — on master-off nothing would ever clear
                    # a re-asserted block. Behavior-identical in normal operation
                    # (the epoch only moves on a hand-back).
                    if self._stopped or epoch != self._epoch:
                        break
                    await self._reconcile_lever(lever, target, state)
                # B2: drop decisions for levers no longer opined on (so the
                # diagnostic reflects only this cycle's active levers).
                self._lever_decisions = {
                    k: v for k, v in self._lever_decisions.items() if k in desired
                }
                # The fan-off watchdog counts one CONTINUOUS commanded-on episode:
                # a lever that left the merge (zone released/disabled/season flip)
                # must forget its count, or a later episode resumes past the
                # threshold and the == crossing never fires again (disarmed).
                self._fan_off = {
                    k: v for k, v in self._fan_off.items() if k in desired
                }
            else:
                # No actuating pass -> no episode continuity either (master-off /
                # deploy-dark gaps would otherwise freeze a stale count).
                self._fan_off.clear()
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
        cfg = state.config
        desired = merge_desired(pure_outputs)
        run_plan = _make_run_plan(cfg, self._forecast, state.now, state.outdoor_temp)
        duty_state = self._cooling.duty if self._cooling else DutyState()
        plan = build_plan(
            state, run_plan, desired, duty_state,
            list(self._forecast), cfg.lookahead,
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
            peak_ratio=cfg.regime_peak_ratio, medium_ratio=cfg.regime_medium_ratio,
        )
        # F3b: per-room 12h forward simulation + pre-cool (pure, plan-only).
        solar_curve, solar_model = self._solar_forecast(state)
        trajectories = build_room_plans(
            state, self._room_params(state), list(self._forecast),
            solar_curve, cfg.lookahead,
            dt_min=PLAN_SIM_STEP_MIN, downsample_min=PLAN_SIM_DOWNSAMPLE_MIN,
            max_precool_depth=cfg.precool_max_depth,
        )
        # F4c: surface the CACHED unified reference schedule (already attached to
        # the state by _maybe_refresh_schedule) — the same one the band controller
        # reads, so the sensor and the drive path never disagree.
        return replace(
            plan, regime=regime, g_house=load.g_house, k_house=load.k_house,
            load_ratio=load.load_ratio, room_trajectories=trajectories,
            solar_model=solar_model,
            center_compositions=self._center_compositions(state),
            center_schedule=state.center_schedule,
        )

    def _maybe_refresh_schedule(self, state: HouseState) -> HouseState:
        """F4c Phase 6: keep a SLOW-moving cached reference schedule + attach it (and
        the planner-drive gate) to the state. Recompute at the forecast cadence, on
        an effective-mode change (so a #8 Via->Casa transition re-plans promptly), or
        when we have none yet — NOT every 30 s tick (the fast loop reads it forward
        via CenterSchedule.at). Computed regardless of the switch (for the plan-only
        sensor); the switch only gates whether the band controller USES it."""
        mode_key = (state.house_mode, state.mode_offset)
        due = (
            self._center_schedule_cache is None
            or self._schedule_ts is None
            or state.now - self._schedule_ts >= FORECAST_REFRESH
            or mode_key != self._schedule_mode
        )
        if due:
            solar_curve, solar_model = self._solar_forecast(state)
            self._center_schedule_cache = self._center_schedule(
                state, solar_curve, solar_model
            )
            self._schedule_ts = state.now
            self._schedule_mode = mode_key
        return replace(
            state,
            center_schedule=self._center_schedule_cache,
            unified_planner_enabled=unified_planner_enabled(self.hass, self.entry),
        )

    def _center_schedule(self, state: HouseState, solar_curve, solar_model):
        """F4c Phase 5: build the unified per-leader band-center reference schedule
        by composing the shipping pure cores (schedule_precool / energy_precool /
        run_rest_durations / return_lead_time). PLAN-ONLY — returned on the plan
        view, consumed by nothing. Best-effort: never breaks the plan view."""
        cfg = state.config
        try:
            # PV shaping needs a REAL solar forecast + a PV-remaining reading;
            # otherwise the reference is just base + #9 pre-cool.
            pv_kwh = _num(self.hass, CONDOMINIO_PV_REMAINING)
            local = dt_util.now()
            frac = max(0.0, (1440 - (local.hour * 60 + local.minute)) / 1440.0)
            pv_active = solar_model != "flat" and pv_kwh is not None
            return plan_center_schedule(
                state, self._room_params(state), list(self._forecast), solar_curve,
                lookahead=cfg.lookahead, max_precool_depth=cfg.precool_max_depth,
                pv_active=pv_active, pv_kwh_remaining=pv_kwh,
                consumption_kwh_remaining=cfg.pv_daily_need_kwh * frac,
                pv_floor_rich=cfg.pv_floor_rich, pv_floor_poor=cfg.pv_floor_poor,
                pv_coast_relax=cfg.pv_coast_relax, pv_eff_fraction=cfg.pv_eff_fraction,
                pv_eff_min=cfg.pv_eff_min,
                eta=getattr(self.away_return, "eta", None),
                return_max_lead=cfg.return_max_lead, return_margin=cfg.return_margin,
                dt_min=PLAN_SIM_STEP_MIN,
            )
        except Exception:  # noqa: BLE001 - plan-only reference must never break the view
            _LOGGER.debug("Center-schedule build failed", exc_info=True)
            return None

    def _center_compositions(self, state: HouseState) -> dict:
        """F4c Phase 1 observability: the resolved band center per eligible cooling
        leader. R1: read from the annotated ZoneSnapshot fields — the SAME
        resolution the FanBandController slams — instead of a second copy of the
        eligibility + compose logic (the drift hazard R1 kills). Visible even
        deploy-dark, so each feature's contribution to the center is inspectable
        before actuation lights up."""
        if state.house_setpoint is None or state.mode_offset is None:
            return {}
        base = state.house_setpoint + state.mode_offset
        return {
            z.zone_id: {
                "center": round(z.resolved_center, 2),
                "base": round(base, 2),
                "source": z.center_source,
                "floored": z.center_floored,
                "planner_driven": z.planner_driven,
            }
            for z in state.zones.values()
            if z.resolved_center is not None
        }

    def _check_unresolved_centers(self, state: HouseState) -> None:
        """R1 loud fallback: an eligible cooling leader reaching an ACTUATING pass
        with no resolved_center means the annotate_centers call in _cycle was lost
        or misordered — the band would silently degrade to the base center (erasing
        a live pre-cool/PV shift without failing anything). WARN once per zone on
        the transition (pattern of _track_stale_temp); a resolved cycle clears it."""
        if state.house_setpoint is None or state.mode_offset is None:
            return
        free = _is_free_cooling(state)
        for z in state.zones.values():
            eligible = (
                _is_cooling_leader(z) and z.enabled and not z.paused and not free
                and not (z.bedroom and state.night_active)
            )
            if not eligible or z.resolved_center is not None:
                self._unresolved_center.discard(z.zone_id)
                continue
            if z.zone_id not in self._unresolved_center:
                self._unresolved_center.add(z.zone_id)
                _LOGGER.warning(
                    "Cooling leader %s reached an actuating pass with no resolved "
                    "band center — annotate_centers is missing/misordered in "
                    "_cycle; band control is degrading to the base center",
                    z.zone_id,
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
                # mirror the live RUN-fan sizing law so plan == actuation
                pulldown_hours=COOL_PULLDOWN_HOURS,
                run_floor=COOL_RUN_FAN_FLOOR,
                peak_outdoor=state.duty_peak_outdoor,
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
        n = int(state.config.lookahead.total_seconds() / 60 // PLAN_SIM_STEP_MIN) + 1
        if not state.config.solar_forecast_enabled:
            cur = state.solar if state.solar is not None else 0.0
            return [cur] * n, "flat"
        if _AstralObserver is None:  # astral unavailable -> flat fallback
            cur = state.solar if state.solar is not None else 0.0
            return [cur] * n, "flat"
        try:
            obs = _AstralObserver(
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
            cfg = state.config
            pv_kwh = _num(self.hass, CONDOMINIO_PV_REMAINING)
            local = dt_util.now()  # LOCAL day clock (state.now is UTC)
            frac_remaining = max(0.0, (1440 - (local.hour * 60 + local.minute)) / 1440.0)
            raw = energy_precool_decision(
                effectiveness=eff, now_index=0,
                pv_kwh_remaining=pv_kwh,
                consumption_kwh_remaining=cfg.pv_daily_need_kwh * frac_remaining,
                eff_fraction=cfg.pv_eff_fraction,
                eff_min=cfg.pv_eff_min,
                floor_rich=cfg.pv_floor_rich,
                floor_poor=cfg.pv_floor_poor,
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
        return replace(
            state, pv_mode=decision.mode, pv_floor=decision.floor,
            pv_coast_relax=state.config.pv_coast_relax,
        )

    def _track_fan_off(self, lever: str, desired, current) -> None:
        """Watchdog: count consecutive actuating cycles a fan is commanded >0%
        but reads OFF (delivered 0). The reconcile re-asserts on its own; this
        exists because the failure mode is INVISIBLE comfort loss — the valve
        interlock keeps the zone from cooling while every setpoint looks right
        (padronale 2026-07-02/03). WARN once on the crossing; recovery clears."""
        try:
            want_on = desired is not None and float(desired) > 0
        except (TypeError, ValueError):
            want_on = False
        if not want_on or current != 0:
            self._fan_off.pop(lever, None)
            return
        n = self._fan_off.get(lever, 0) + 1
        self._fan_off[lever] = n
        if n == RUN_FAN_OFF_WARN_CYCLES:  # log once, on the crossing
            _LOGGER.warning(
                "Fan %s has been commanded to %s%% but has read OFF for %d "
                "consecutive cycles — its zone is NOT cooling (the KNX fancoil "
                "interlock holds the valve closed while the fan is off)",
                lever.partition(":")[2], desired, n,
            )

    async def _reconcile_lever(self, lever: str, target, state: HouseState) -> None:
        current = self._read_current(lever)
        if lever.startswith("fan:"):
            self._track_fan_off(lever, target, current)
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
        # B2: record the decision + warn on the transition INTO a manual-override
        # concession (the #1 documented robustness risk on this lossy KNX bus —
        # a lever we've handed back to a human until the backoff expires).
        prev = self._lever_decisions.get(lever)
        if result.note == "override" and (prev is None or prev.get("note") != "override"):
            _LOGGER.warning(
                "Lever %s conceded to a manual override until %s "
                "(wanted %s, read %s) — not re-asserting until the backoff expires",
                lever, result.state.override_until, target, current,
            )
        self._lever_decisions[lever] = {
            "note": result.note,
            "desired": target,
            "current": current,
            "written": result.state.written,
            "attempts": result.state.attempts,
            "override_until": result.state.override_until,
            "wrote": result.write,
        }
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
            # Compare on the DELIVERED airflow, not the retained % attribute: an
            # OFF fan still reports the last bus percentage, so a fan silenced
            # overnight read "satisfied" against a RUN command with the same %
            # and was never turned back on — and the KNX fancoil controller
            # holds the EV valve CLOSED while the fan is off, so the zone never
            # cooled (padronale, proven live 2026-07-02/03). OFF delivers 0%;
            # anything not on/off stays None → reconcile treats it as transient.
            if s.state == STATE_OFF:
                return 0
            if s.state == STATE_ON:
                return s.attributes.get(ATTR_PERCENTAGE)
            return None
        if kind == "cover":
            # position-controlled shading: compare on current_position (0-100).
            pos = s.attributes.get(ATTR_CURRENT_POSITION)
            if pos is not None:
                return pos
            # B4: a cover with no position support reports only open/closed — map
            # it so reconcile can still compare against a numeric target
            # (0 = closed/down, 100 = open). Anything else stays None → transient.
            if s.state == STATE_CLOSED:
                return 0
            if s.state == STATE_OPEN:
                return 100
            return None
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
            # Assert the on/off STATE together with the % — set_percentage alone
            # left an off fan off when the bus already held the commanded %
            # (the read-side blindness above), and the KNX valve interlock then
            # kept the zone from cooling. These fans have SEPARATE switch and
            # speed group objects (knx_fancoil_all.yaml: switch 5/0/x, speed
            # 5/4/x; xknx turn_off writes ONLY the switch GA), so a 0-command
            # dispatches BOTH: turn_off asserts the verifiable OFF state, then
            # set_percentage(0) disarms the retained speed so an external ON
            # (wall press) resumes silent, not at the last RUN %.
            pct = int(float(value))
            if pct > 0:
                await self._call(
                    FAN_DOMAIN, SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity, ATTR_PERCENTAGE: pct},
                )
            else:
                await self._call(
                    FAN_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity}
                )
                await self._call(
                    FAN_DOMAIN, SERVICE_SET_PERCENTAGE,
                    {ATTR_ENTITY_ID: entity, ATTR_PERCENTAGE: 0},
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
        # C5: bound the write so a wedged KNX call can't stall the whole cycle. On
        # timeout the reconcile re-asserts next cycle. wait_for cancels the inner
        # call, so a partial write self-heals on the next pass.
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(domain, service, data, blocking=True),
                LEVER_CALL_TIMEOUT,
            )
        except TimeoutError:
            _LOGGER.warning(
                "Lever write %s.%s (%s) timed out after %ss; moving on",
                domain, service, data.get(ATTR_ENTITY_ID), LEVER_CALL_TIMEOUT,
            )

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
        # B4: `_forecast_temp_at` / `_forecast_cloud_at` early-break assuming the
        # lists are time-sorted; the weather integration usually returns them so,
        # but sort defensively so an out-of-order response can't truncate the scan.
        parsed.sort(key=lambda wt: wt[0])
        clouds.sort(key=lambda wc: wc[0])
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

    async def _restore_presets(self) -> None:
        """Fail-safe (B1): hand every zone the supervisor slammed into
        building_protection back to the neutral `auto` preset, so no zone is ever
        left unable to condition with no supervisor alive (the `_startup_resync`
        self-heals a normal restart, but NOT integration removal/disable).

        Only touches zones currently in building_protection — the specific harm the
        invariant names ("no lingering building_protection"). SKIPS zones that
        SHOULD stay in it: #10-disabled and window-paused. The caller holds
        self._lock; each write is guarded (the fail-safe must never raise).

        NB: a KNX setpoint the #3 band may have slammed is NOT restored here — there
        is no recorded native baseline to restore it to; preset=auto hands local
        scheduling back. Tracked separately in the engine-hardening backlog.
        """
        window = getattr(self.coordinator, "window", None)
        paused = getattr(window, "paused", None) or set()
        for zone_id, zone in ZONES.items():
            climate = zone.get("climate")
            if not climate or zone.get("emitter") not in PRESET_CONTROLLABLE_EMITTERS:
                continue
            state = self.hass.states.get(climate)
            if (
                state is None
                or state.attributes.get(ATTR_PRESET_MODE) != PRESET_BUILDING_PROTECTION
            ):
                continue  # only un-stick a LINGERING building_protection
            if zone_id in paused or is_zone_disabled(self.hass, self.entry, zone_id):
                continue  # #10-disabled / window-paused zones SHOULD stay in BP
            try:
                await self._call(
                    CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE,
                    {ATTR_ENTITY_ID: climate, ATTR_PRESET_MODE: PRESET_AUTO},
                )
            except Exception:  # noqa: BLE001 - fail-safe must not raise
                _LOGGER.exception("Fail-safe: could not restore preset for %s", climate)

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
        # Invalidate any cycle already queued on the lock: it captured the old epoch
        # and will abort after we release, so it can't re-block/re-slam post-hand-back
        # (works even on master-OFF, where the engine stays alive and `_stopped` is
        # never set). Bump BEFORE acquiring so a queued cycle sees the new value.
        self._epoch += 1
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
            # B1: un-stick any zone left in building_protection (skip #10-disabled +
            # window-paused, which SHOULD stay in it) -> neutral `auto` preset.
            await self._restore_presets()
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
