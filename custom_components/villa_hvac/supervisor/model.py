"""Pure house-state model (C2 split): the per-cycle HouseState/ZoneSnapshot/
CoverInfo data carriers + the cooling-leader / free-cool helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # runtime-pure: the annotations never resolve at import time
    from ..supervisor_config import SupervisorConfig
    from .planner import CenterSchedule



# --- House-state model (pure data) -------------------------------------------
# The Supervisor builds one snapshot per cycle; policies read it and return
# desired lever settings. Keep this a plain data carrier — building it from
# Home Assistant lives in engine.py so this module stays import-pure.


@dataclass(frozen=True)
class ZoneSnapshot:
    """Per-zone slice of the house state."""

    zone_id: str
    name: str
    climate: str | None
    emitter: str | None
    temp: float | None = None      # fused current temperature (#1)
    demand: bool | None = None     # EV FAN valve open = actually cooling
    enabled: bool = True           # #10 zone enable switch
    paused: bool = False           # #4 window pause
    bedroom: bool = False          # camere silenziose zone (#2b)
    fan_min: int = 0               # rest/min-circulation fan % for this zone (#3 v2)
    setpoint_offset: float = 0.0   # #2: °C added to this zone's base center (per-room trim)
    fancoil: str | None = None     # primary fan entity
    manuale: str | None = None     # primary manuale switch entity
    follows: str | None = None     # leader zone_id this zone defers to (open-space)
    # All (fan, manuale) units this leader drives at one speed — e.g. living_room
    # owns both Salotto and Cucina fancoils (one open space, #3 v2).
    fancoil_units: tuple[tuple[str, str], ...] = ()
    # F2: blended (prior→learned) thermal model for this zone + confidences.
    # None until the estimator/store populates them; control falls back to priors.
    model_a: float | None = None
    model_b: float | None = None
    model_c: float | None = None
    model_k: float | None = None
    model_confidence: float | None = None    # min(abc, k) confidence, for display
    model_k_confidence: float | None = None   # k-only confidence (regime/F2b gating)
    # D1: may the unified planner's reference drive this room's center? (abc
    # solar-excited + identified AND k converged). Hard gain-limited rooms stay
    # False -> their planner trajectory is ADVISORY (comfort held by the band).
    model_planner_eligible: bool = False
    # F2b: live actuation state, so the estimator can learn k only on held-fan
    # windows (manuale on + known %) — never from AUTO/unknown fan.
    fan_pct: int | None = None
    manuale_on: bool = False
    # F4b: °C to add to this zone's band center right now (outside its comfort
    # window). Capped by the engine so center+relax never exceeds duty_comfort_max.
    comfort_relax: float = 0.0
    # S_eff (STORY_SEFF): the per-zone effective irradiance every b-consumer
    # reads (W/m²-equivalent, GHI scale). While the feature flag is off the
    # engine populates the house GHI here with source/units "ghi", so consumers
    # switched to z.s_eff stay byte-identical to the GHI era. s_eff_source is
    # the per-cycle quality ("facade" | "facade_degraded" | "ghi" | "fallback" —
    # the estimator learns only from the first and third); s_eff_units is the
    # stable semantics stamp the migration rebase compares (e.g.
    # "seff1:225x1,292x1").
    s_eff: float | None = None
    s_eff_source: str = "ghi"
    s_eff_units: str = "ghi"
    # R1 (Tier-1): the ONE resolved band center every consumer reads — planner
    # reference ▸ compose_center ladder ▸ base — written once per cycle by the
    # engine's annotate_centers call (planner.resolve_center). Zones outside the
    # band eligibility (not a leader / disabled / paused / free-cool / bedroom
    # under camere silenziose / no base center) keep these defaults.
    resolved_center: float | None = None
    center_source: str = "none"      # planner|base|pv_bank|pv_coast|precool|comfort_relax
    center_floored: bool = False     # the ladder's comfort floor clamped a lowering feature
    planner_driven: bool = False     # the unified planner reference drove the center
    # Split-AC trio (#6): the standard Daikin `climate` head for this zone + its
    # live state, populated for zones carrying an `ac_group`. `split_climate` is
    # the head entity (resolved: the zone's explicit `split_climate`, else its
    # `climate` when emitter=="split_ac"); the rest are the live reads. These heads
    # share ONE compressor (single refrigerant direction) — the SplitGroupController
    # reasons over the group, never through the fancoil cooling stack.
    ac_group: str | None = None       # e.g. "split_trio" — group membership key
    split_climate: str | None = None  # the split head entity_id
    split_role: str | None = None     # storage (cantina) | comfort (palestra) | manual (garage)
    split_mode: str | None = None     # live hvac_mode (off/cool/dry/fan_only/heat/auto)
    split_setpoint: float | None = None   # live target temperature
    split_fan_mode: str | None = None      # live fan_mode (off/low/medium/high)
    split_temp: float | None = None        # the split head's own current_temperature
    occupied: bool | None = None      # EP occupancy (ep_occ), None if no/stale sensor
    humidity: float | None = None     # RH % (humidity_sensor), None if no/stale sensor



@dataclass(frozen=True)
class CoverInfo:
    """A shadeable cover, resolved from the registries (#6).

    `target_position` is the per-room shade target (HA cover position: 0 = fully
    closed/down, 100 = open) the blind is driven to when shading triggers; None
    means "use the house default". `blocked` is the per-room manual override —
    when True the cover is skipped entirely (not closed, not reopened).
    """

    entity_id: str
    orientation: str            # north / east / south / west (device label)
    zone: str | None = None     # area_id
    floor: str | None = None    # area.floor_id
    target_position: int | None = None  # per-room shade target (HA position)
    blocked: bool = False       # per-room manual override -> skip shading
    # Live position (0 = down, 100 = open); None = unknown this cycle. The
    # never-raise invariant needs it: shading commands min(current, target).
    current_position: int | None = None



@dataclass(frozen=True)
class HouseState:
    """Unified per-cycle snapshot the policy stack reasons over."""

    now: datetime
    zones: dict[str, ZoneSnapshot] = field(default_factory=dict)
    covers: tuple[CoverInfo, ...] = ()
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    shading_enabled: bool = False
    shading_solar_threshold: float | None = None
    shading_default_position: int | None = None  # #6 fallback shade position
    shading_proportional: bool = False  # #6: scale shade depth by solar (+ heat)
    band_width: float | None = None    # #3 v2 comfort band B (°C)
    band_slam: float | None = None     # #3 v2 setpoint slam A (°C)
    model_learning_enabled: bool = True  # F2 online estimator observer
    duty_enabled: bool = False          # #9 duty-cycle switch
    duty_max_stint: timedelta | None = None
    duty_cooloff: timedelta | None = None
    duty_comfort_max: float | None = None  # abort cooloff if a zone exceeds this
    comfort_floor: float | None = None  # F4c: lower bound; lowering features can't go below
    duty_peak_outdoor: float | None = None  # at/above this outdoor temp -> no duty
    precool: bool = False               # #9 forecast: hot peak imminent
    precool_offset: float | None = None  # °C below target while pre-cooling
    # PV/energy-aware daily pre-cool (F4c-lite). pv_mode = bank/coast/hold/None; the
    # band controller drives center -> pv_floor on BANK, center + pv_coast_relax
    # (capped at duty_comfort_max) on COAST. None = no PV opinion (normal band).
    pv_mode: str | None = None
    pv_floor: float | None = None
    pv_coast_relax: float = 0.0
    night_active: bool = False          # #2b camere silenziose in effect
    fan_pacing_enabled: bool = False    # #3 fan pacing switch
    comfort_enabled: bool = False       # F4b comfort windows active (for PV COAST)
    season: str | None = None          # summer / winter
    house_mode: str | None = None      # Casa / Via / Notte / Vacanza
    auto_setback: bool = True          # #2 global Auto setback switch
    house_setpoint: float | None = None  # dashboard slider base setpoint
    mode_offset: float | None = None   # season-aware offset for house_mode
    free_cool_enabled: bool = False    # #5 outdoor free-cooling shutoff
    free_cool_threshold: float | None = None  # outdoor below this -> suppress
    # Windows → free-cool inference (v0.56.0): open window-CONTACT count + zone
    # ids, and the resolved "the house is being aired" verdict (switch on +
    # summer + count ≥ threshold + outdoor ≤ indoor − margin), computed by
    # build_house_state. `windows_free_cool` ORs into `_is_free_cooling`, so the
    # whole #5 coast stack follows it.
    windows_open: tuple[str, ...] = () # zone ids with an OPEN window contact
    windows_free_cool: bool = False    # airing verdict -> coast like #5
    outdoor_temp: float | None = None  # Ecowitt gw3000a
    solar: float | None = None         # Ecowitt solar radiation W/m²
    consenso_freddo: str | None = None
    consenso_caldo: str | None = None
    blocco: str | None = None          # central BLOCCO switch state
    # C3: the parsed-once options snapshot (SupervisorConfig). The clean config half
    # the planner reads; None in bare-constructed test states. Runtime-pure (the
    # type is a TYPE_CHECKING import), read duck-typed by attribute.
    config: "SupervisorConfig | None" = None
    # F4c Phase 6: the cached unified band-center REFERENCE schedule + whether the
    # planner may DRIVE the center this cycle (switch on). The FanBandController
    # reads `center_schedule.at(zone, now)` for planner-eligible rooms when
    # `unified_planner_enabled`; else the compose_center ladder. None = no schedule.
    center_schedule: "CenterSchedule | None" = None
    unified_planner_enabled: bool = False
    # #6 split-AC trio: the opt-in flag + the parsed config the SplitGroupController
    # reads (cool-side only; cantina self-regulates at its setpoint). min_on/min_off
    # are the per-head anti-short-cycle dwell (C4).
    split_enabled: bool = False
    split_cantina_setpoint: float | None = None
    split_palestra_setpoint: float | None = None
    split_min_on: timedelta = timedelta(minutes=5)
    split_min_off: timedelta = timedelta(minutes=3)
    split_rh_ceiling: float = 65.0    # run `dry` above this; wine humidity ceiling
    split_rh_floor: float = 55.0      # relax the cantina setpoint below this (avoid over-drying)



# --- #11 plan view (pure) ----------------------------------------------------
# Project the organism's next-12h INTENT into a single structured view a
# dashboard can render: the forecast curve + peak, the pre-cool / peak-skip /
# duty run-rest regime, and each zone's planned setpoint. Pure so it is fully
# unit-testable and so it can be computed every cycle (read-only) even while the
# supervisor is deploy-dark — letting us watch the plan before lighting up the
# actuation. `desired` is the merged output of the PURE policy stack only (no
# stateful controllers), so computing it never advances duty/pacing timers.

# Season string (mirror of const.SEASON_SUMMER), kept local so this module
# stays import-pure (const.py imports homeassistant).
_SEASON_SUMMER = "summer"



def _is_free_cooling(state: HouseState) -> bool:
    # v0.56.0: an open-windows airing verdict (owner rule 2) coasts exactly like
    # the #5 outdoor threshold — one free-cool concept downstream.
    if state.windows_free_cool:
        return True
    return (
        state.free_cool_enabled
        and state.season == _SEASON_SUMMER
        and state.outdoor_temp is not None
        and state.free_cool_threshold is not None
        and state.outdoor_temp < state.free_cool_threshold
    )



def _is_cooling_leader(z: ZoneSnapshot) -> bool:
    """A cooling fancoil LEADER: owns a thermostat + its fancoil units and is not
    a follower (open-space followers like Cucina are driven by their leader).
    The single shared definition for FanBandController / ThermalEstimator / the
    regime index / the planner, so the set never drifts between them."""
    return bool(
        z.climate and z.emitter == "fancoil" and z.fancoil_units and not z.follows
    )



def active_cooling_leaders(state: HouseState) -> list[ZoneSnapshot]:
    """Cooling leaders currently under active management this cycle: a leader that
    is enabled (#10), not window-paused (#4), not a bedroom owned by camere
    silenziose (#2b), and reporting a temperature.

    The SINGLE definition shared by the duty comfort-breach and the coalescing
    coordinator, so the two can never disagree on which rooms 'count'. Note it
    deliberately EXCLUDES the non-cooled zones (radiant baths, split-AC rooms)
    that carry a fused temp but no fancoil — a warm bathroom must not, e.g.,
    force the duty cooloff to abort forever.
    """
    return [
        z
        for z in state.zones.values()
        if _is_cooling_leader(z)
        and z.enabled
        and not z.paused
        and not (z.bedroom and state.night_active)
        and z.temp is not None
    ]



# --- Split-AC group helpers (#6, pure) ---------------------------------------
# The trio shares ONE outdoor heat pump that cannot make heat and cold at once.
# `cool` and `dry` are the same refrigerant direction (compatible); `fan_only`
# needs no compressor (neutral); only `heat` conflicts. The gateway (Zennio
# KLIC-DD) gives NO standby/conflict feedback, so a head forced to standby still
# reports its requested mode — we must detect an incompatible mix ourselves.

SPLIT_COOL_MODES: tuple[str, ...] = ("cool", "dry")  # same refrigerant direction
SPLIT_HEAT_MODE = "heat"
SPLIT_NEUTRAL_MODES: tuple[str, ...] = ("off", "fan_only", "fan")  # no compressor call



def split_members(state: HouseState, group: str | None = None) -> list[ZoneSnapshot]:
    """Zones belonging to a split-AC group (all groups if `group` is None),
    in a stable insertion order."""
    return [
        z
        for z in state.zones.values()
        if z.ac_group is not None and (group is None or z.ac_group == group)
    ]



def split_mode_conflict(members: list[ZoneSnapshot]) -> bool:
    """True if the heads' live modes are physically unsatisfiable on one shared
    compressor: at least one calling `heat` while another calls `cool`/`dry`.
    `fan_only`/`off` are neutral and never conflict. This is the case the KNX bus
    cannot report (the losing head keeps echoing its requested mode)."""
    modes = {(z.split_mode or "").lower() for z in members}
    wants_heat = SPLIT_HEAT_MODE in modes
    wants_cool = any(m in SPLIT_COOL_MODES for m in modes)
    return wants_heat and wants_cool
