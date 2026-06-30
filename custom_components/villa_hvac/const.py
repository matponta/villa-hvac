"""Constants and zone map for the Villa HVAC orchestration integration.

Verified against the live Home Assistant on 2026-06-23.
The real PdC call signals are KNX binary_sensors (NOT climate hvac_action):
cooling consenso turns on when any fancoil fan > 0.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "villa_hvac"

# Diagnostics + control platforms (#10 zone disable, #1 temp, #2a house mode).
PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# --- KNX climate presets -----------------------------------------------------
# Setting a thermostat to building_protection drives its fancoil fan to 0 while
# keeping frost/building protection active (the lever for disabling a zone).
PRESET_BUILDING_PROTECTION = "building_protection"
# Preset to restore when a zone is re-enabled and we have no captured prior one.
PRESET_DEFAULT_ENABLED = "comfort"

# --- House mode (#2a) --------------------------------------------------------
# Integration-owned house-mode select; drives a KNX preset on every thermostat
# zone. Mapping verified live 2026-06-23 (each preset carries its own setpoint
# offset: comfort~24, standby +2, economy +4, building_protection = frost/off).
# Option strings match the legacy input_select.modalita_casa for easy migration.
HOUSE_MODE_HOME = "Casa"
HOUSE_MODE_AWAY = "Via"
HOUSE_MODE_NIGHT = "Notte"
HOUSE_MODE_VACATION = "Vacanza"
HOUSE_MODES: list[str] = [
    HOUSE_MODE_HOME,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_NIGHT,
    HOUSE_MODE_VACATION,
]
MODE_PRESET: dict[str, str] = {
    HOUSE_MODE_HOME: "comfort",
    HOUSE_MODE_AWAY: "standby",
    HOUSE_MODE_NIGHT: "economy",
    HOUSE_MODE_VACATION: PRESET_BUILDING_PROTECTION,
}

# When a mode is applied we also push set_temperature = house_setpoint + offset.
# Setback DEPTH differs by season (cooling wants deeper away setback than
# heating), so offsets are season-specific and editable in the options flow.
# Casa is always +0; Vacanza maps to building_protection (frost) -> no setpoint.
SEASON_AUTO = "auto"
SEASON_SUMMER = "summer"
SEASON_WINTER = "winter"
# Reference thermostat for auto season detection (state "heat" -> winter).
SEASON_REFERENCE_CLIMATE = "climate.salotto_termostato_2"

OPT_SEASON = "season"
OPT_SUMMER_VIA_OFFSET = "summer_via_offset"
OPT_SUMMER_NOTTE_OFFSET = "summer_notte_offset"
OPT_WINTER_VIA_OFFSET = "winter_via_offset"
OPT_WINTER_NOTTE_OFFSET = "winter_notte_offset"

# Defaults: setpoint = base + offset. Summer (cooling) setback = WARMER (positive
# offset); winter (heating) setback = COOLER (negative offset). So the signs are
# opposite by season.
SEASON_OFFSET_DEFAULTS: dict[str, dict[str, float]] = {
    SEASON_SUMMER: {HOUSE_MODE_AWAY: 5.0, HOUSE_MODE_NIGHT: 3.0},
    SEASON_WINTER: {HOUSE_MODE_AWAY: -2.0, HOUSE_MODE_NIGHT: -4.0},
}
SEASON_OFFSET_OPTS: dict[str, dict[str, str]] = {
    SEASON_SUMMER: {
        HOUSE_MODE_AWAY: OPT_SUMMER_VIA_OFFSET,
        HOUSE_MODE_NIGHT: OPT_SUMMER_NOTTE_OFFSET,
    },
    SEASON_WINTER: {
        HOUSE_MODE_AWAY: OPT_WINTER_VIA_OFFSET,
        HOUSE_MODE_NIGHT: OPT_WINTER_NOTTE_OFFSET,
    },
}

# House comfort setpoint slider (number.villa_hvac_house_setpoint) for dashboards.
DEFAULT_HOUSE_SETPOINT = 24.0
HOUSE_SETPOINT_MIN = 16.0
HOUSE_SETPOINT_MAX = 28.0
HOUSE_SETPOINT_STEP = 0.5

# Zone emitters whose KNX thermostat accepts the comfort/standby/economy ladder
# (the 17 *_termostato_2). Split-AC zones (aircon_*) are excluded from #2.
PRESET_CONTROLLABLE_EMITTERS = ("fancoil", "radiant")

# --- Real call signals (KNX) -------------------------------------------------
CONSENSO_FREDDO = "binary_sensor.ct_consenso_freddo_villa"  # cooling call to PdC
CONSENSO_CALDO = "binary_sensor.ct_consenso_caldo_villa"    # heating call to PdC

# --- #9 valve-based cooling signals (Stage 1; verify entity_ids after reload) -
# The REAL per-room cooling demand is the fancoil chilled-water valve (EV FAN,
# on/off), exposed via knx/knx_fancoil_valves.yaml. ON = valve open = cooling.
# (fan.percentage is NOT demand — it runs constant in AUTO.)
COOL_VALVES: dict[str, str] = {
    "living_room": "binary_sensor.fancoil_salotto_valvola",
    "kitchen": "binary_sensor.fancoil_cucina_valvola",
    "main_bedroom": "binary_sensor.fancoil_camera_padronale_valvola",
    "gabriroom": "binary_sensor.fancoil_camera_gabriele_valvola",
    "studio_v": "binary_sensor.fancoil_camera_ospiti_valvola",
    "sala_giochi": "binary_sensor.fancoil_sala_giochi_valvola",
    "office": "binary_sensor.fancoil_studio_pianerottolo_p1_valvola",
    "stairs_p1": "binary_sensor.fancoil_locale_rack_valvola",  # rack fancoil cools P1
    "rack": "binary_sensor.fancoil_locale_rack_valvola",
}
# Central lever (#9): force-stop the villa cooling call to the PdC.
# WARNING: verify polarity (block vs enable) live before actuating. Observed live
# 2026-06-27: switch OFF while cooling ran normally -> OFF = released (not
# blocking). The fail-safe releases by turning it OFF; confirm with one
# supervised toggle before #9 actuates it.
CONSENSO_BLOCCO = "switch.ct_blocco_freddo_villa"
BLOCCO_RELEASED = "off"  # state that lets the villa cool (fail-safe target)

# --- Outdoor / weather (Ecowitt GW3000A; #5/#6/#9 weather feed-forward) -------
# Richer than the PdC's own probe (solar + rain + humidity); s5a is the fallback.
OUTDOOR_TEMP = "sensor.gw3000a_outdoor_temperature"
OUTDOOR_TEMP_FALLBACK = "sensor.s5a_temperatura_esterna"
SOLAR_RADIATION = "sensor.gw3000a_solar_radiation"

# Cooling demand of a zone == its fancoil fan percentage > 0.
FANCOILS: list[str] = [
    "fan.fancoil_salotto",
    "fan.fancoil_cucina",
    "fan.fancoil_camera_padronale",
    "fan.fancoil_camera_gabriele",
    "fan.fancoil_camera_ospiti",          # Studio V
    "fan.fancoil_sala_giochi",
    "fan.fancoil_studio_pianerottolo_p1",  # Office (Studio)
    "fan.fancoil_locale_rack",             # Rack + Pianerottolo P1 (dual outlet)
]

# --- Zone map (verified live 2026-06-23) -------------------------------------
# Temperature fusion (#1) is THERMOSTAT-PRIMARY: each zone's fused current
# temperature reads `temp_sensor` (the clean `sensor.clima_*` twin of the KNX
# thermostat, or a dedicated room sensor) and falls back to the climate's
# `current_temperature` attribute when the primary is missing/stale.
# EP temperature is intentionally NOT the absolute source: measured
# EP-vs-thermostat offsets are large (~5 C) and vary with time of day
# (see EP_TEMP_OFFSETS). EP entities are recorded here for occupancy (#2) and a
# future EP-primary revisit (TODO: circle back to EP calibration).
#
# The #10 enable switch is created only for zones with emitter == "fancoil"
# (the validated building_protection -> fan 0 lever); radiant / split-AC zones
# are excluded because that lever is not verified for them.
ZONES: dict[str, dict] = {
    "living_room": {
        "name": "Salotto",
        "floor": "terra",
        "climate": "climate.salotto_termostato_2",
        "temp_sensor": "sensor.clima_salotto",
        "fancoils": ["fan.fancoil_salotto", "fan.fancoil_cucina"],  # kitchen follows
        "ep_temp": "sensor.everything_presence_one_626794_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_626794_occupancy",
        "emitter": "fancoil",  # summer
    },
    "kitchen": {
        "name": "Kitchen",
        "floor": "terra",
        "climate": None,  # open-space: follows Salotto thermostat (no own #10 switch)
        "follows": "living_room",
        "temp_sensor": "sensor.clima_salotto",
        "temp_fallback_climate": "climate.salotto_termostato_2",
        "fancoils": ["fan.fancoil_cucina"],
        "ep_temp": "sensor.everything_presence_one_626130_temperature",
        "ep_occ": "binary_sensor.ep_kitchen_occupancy_filtered",
    },
    "main_bedroom": {
        "name": "Camera Padronale",
        "floor": "secondo",
        "climate": "climate.camera_padronale_termostato_2",
        "temp_sensor": "sensor.clima_camera",
        "fancoils": ["fan.fancoil_camera_padronale"],
        "ep_temp": "sensor.ep_main_bedroom_temperature",
        "ep_occ": "binary_sensor.ep_main_bedroom_occupancy",
        "emitter": "fancoil",
        "bedroom": True,  # camere silenziose (#2b)
        "manuale_switch": "switch.fancoil_camera_padronale_manuale",
    },
    "gabriroom": {
        "name": "Camera Gabriele",
        "floor": "secondo",
        "climate": "climate.camera_gabriele_termostato_2",
        "temp_sensor": "sensor.clima_gabri",
        "fancoils": ["fan.fancoil_camera_gabriele"],
        "ep_temp": "sensor.everything_presence_one_a8c8d0_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_a8c8d0_occupancy",
        "emitter": "fancoil",
        "bedroom": True,  # camere silenziose (#2b)
        "manuale_switch": "switch.fancoil_camera_gabriele_manuale",
    },
    "studio_v": {
        "name": "Studio V",
        "floor": "secondo",
        "climate": "climate.studio_v_termostato_2",
        "temp_sensor": "sensor.clima_studio_v",
        "fancoils": ["fan.fancoil_camera_ospiti"],
        "ep_temp": "sensor.everything_presence_one_a8c910_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_a8c910_occupancy",
        "emitter": "fancoil",
    },
    "sala_giochi": {
        "name": "Sala Giochi",
        "floor": "primo",
        "climate": "climate.sala_giochi_termostato_2",
        "temp_sensor": "sensor.clima_sala_giochi",
        "fancoils": ["fan.fancoil_sala_giochi"],
        "ep_temp": "sensor.everything_presence_one_616a74_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_616a74_occupancy",
        "emitter": "fancoil",
    },
    "office": {
        "name": "Office (Studio)",
        "floor": "primo",
        "climate": "climate.studio_termostato_2",
        "temp_sensor": "sensor.clima_studio",
        "fancoils": ["fan.fancoil_studio_pianerottolo_p1"],
        "ep_temp": "sensor.everything_presence_one_a8c850_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_a8c850_occupancy",
        "emitter": "fancoil",
    },
    "stairs_p1": {
        "name": "Pianerottolo P1",
        "floor": "primo",
        "climate": "climate.pianerottolo_p1_termostato_2",
        "temp_sensor": "sensor.clima_pianerottolo_p1",
        "fancoils": ["fan.fancoil_locale_rack"],  # rack fancoil also cools P1
        "ep_temp": "sensor.everything_presence_one_7c4b0c_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_7c4b0c_occupancy",
        "emitter": "fancoil",
    },
    "rack": {
        "name": "Locale Rack",
        "floor": "primo",
        "climate": None,  # no thermostat/EP -> no #10 switch; cooled by P1 rack fancoil
        "temp_sensor": "sensor.rack_t_h_temperature",  # dedicated rack T/H probe
        "fancoils": ["fan.fancoil_locale_rack"],
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "fancoil",
    },
    # --- Radiant-only zones (winter heating; cooling not fancoil-gated) --------
    "pianerottolo_p2": {
        "name": "Pianerottolo P2",
        "floor": "secondo",
        "climate": "climate.pianerottolo_p2_termostato_2",
        "temp_sensor": "sensor.clima_pianerottolo_p2",
        "ep_temp": "sensor.everything_presence_one_7c59ac_temperature",  # MEDIUM conf
        "ep_occ": "binary_sensor.everything_presence_one_7c59ac_occupancy",
        "emitter": "radiant",
    },
    "ingresso": {
        "name": "Ingresso",
        "floor": "terra",
        "climate": "climate.ingresso_termostato_2",
        "temp_sensor": "sensor.clima_ingresso",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
    },
    "lavanderia": {
        "name": "Lavanderia",
        "floor": "terra",
        "climate": "climate.lavanderia_termostato_2",
        "temp_sensor": "sensor.clima_lavanderia",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
        "window": "cover.vasistas_lavanderia",  # #4 window pause
    },
    "bagno_gabriele": {
        "name": "Bagno Gabriele",
        "floor": "secondo",
        "climate": "climate.bagno_gabriele_termostato_2",
        "temp_sensor": "sensor.clima_bagno_gabriele",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
        "window": "cover.vasistas_gabriele",  # #4: vasistas is in the bathroom
    },
    "bagno_giochi": {
        "name": "Bagno Giochi",
        "floor": "primo",
        "climate": "climate.bagno_giochi_termostato_2",
        "temp_sensor": "sensor.clima_bagno_giochi",
        "ep_temp": "sensor.everything_presence_one_5a7d68_temperature",  # MEDIUM conf
        "ep_occ": "binary_sensor.everything_presence_one_5a7d68_occupancy",
        "emitter": "radiant",
        "window": "cover.vasistas_bagno_sala_giochi",  # #4 window pause
    },
    "bagno_ingresso": {
        "name": "Bagno Ingresso",
        "floor": "terra",
        "climate": "climate.bagno_ingresso_termostato_2",
        "temp_sensor": "sensor.clima_bagno_ingresso",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
    },
    "bagno_padronale_01": {
        "name": "Bagno Padronale 1",
        "floor": "secondo",
        "climate": "climate.bagno_padronale_01_termostato_2",
        "temp_sensor": "sensor.clima_bagno_padronale_01",
        # EP 626788 covers the shared bagno_camera area (LOW conf; shared with _02)
        "ep_temp": "sensor.everything_presence_one_626788_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_626788_occupancy",
        "emitter": "radiant",
    },
    "bagno_padronale_02": {
        "name": "Bagno Padronale 2",
        "floor": "secondo",
        "climate": "climate.bagno_padronale_02_termostato_2",
        "temp_sensor": "sensor.clima_bagno_padronale_02",
        "ep_temp": "sensor.everything_presence_one_626788_temperature",  # shared, LOW
        "ep_occ": "binary_sensor.everything_presence_one_626788_occupancy",
        "emitter": "radiant",
    },
    "bagno_palestra": {
        "name": "Bagno Palestra",
        "floor": "terra",
        "climate": "climate.bagno_palestra_termostato_2",
        "temp_sensor": "sensor.clima_bagno_palestra",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
    },
    # --- Split-AC trio (share ONE compressor -> must run in the same mode) -----
    "palestra": {
        "name": "Palestra",
        "floor": "terra",
        "climate": "climate.palestra_termostato_2",     # radiant thermostat
        "temp_sensor": "sensor.clima_palestra",
        "split_climate": "climate.aircon_palestra_2",   # split AC (trio)
        "ac_group": "split_trio",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
    },
    "cantina_vini": {
        "name": "Cantina Vini",
        "floor": "terra",
        "climate": "climate.aircon_cantina_vini_2",     # split AC (trio)
        "temp_sensor": None,  # no clima_* twin -> uses climate current_temperature
        "ac_group": "split_trio",
        "ep_temp": "sensor.ep_cantina_vini_temperature",
        "ep_occ": "binary_sensor.ep_cantina_vini_occupancy",
        "emitter": "split_ac",
    },
    "garage": {
        "name": "Garage",
        "floor": "terra",
        "climate": "climate.aircon_garage_2",           # split AC (trio)
        "temp_sensor": None,  # no clima_* twin -> uses climate current_temperature
        "ac_group": "split_trio",
        "ep_temp": None,  # EP 3febe8 is 'Garage Grande' (area-ambiguous) -> unmapped
        "ep_occ": None,
        "emitter": "split_ac",
    },
}

# 3-stage fancoil speeds observed (KNX): low/med/high
FAN_STAGES = (33, 67, 100)

# A temperature source older than this is treated as stale -> fall back (#1).
TEMP_STALE_AFTER = timedelta(minutes=30)

# Measured EP-vs-thermostat offsets (offset = thermostat - EP), live 2026-06-23.
# UNUSED today: #1 is thermostat-primary, so EP is not the absolute source.
# Kept for the planned EP-primary revisit. Most zones show a large,
# time-of-day-correlated bias (the EP self-heats while occupied), so a single
# static offset is only valid where the daily swing is small.
#   verdict "static" = safe to apply as a constant; "time" = needs a
#   time-varying correction before EP can be trusted as the absolute source.
EP_TEMP_OFFSETS: dict[str, dict] = {
    "living_room": {"offset": -4.9, "verdict": "time"},
    "kitchen": {"offset": -4.4, "verdict": "time"},
    "main_bedroom": {"offset": -2.2, "verdict": "time"},
    "gabriroom": {"offset": 0.7, "verdict": "time"},
    "studio_v": {"offset": -0.7, "verdict": "time"},
    "sala_giochi": {"offset": -2.1, "verdict": "static"},
    "office": {"offset": -5.0, "verdict": "time"},  # tight stddev -> usable as static
    "stairs_p1": {"offset": 2.6, "verdict": "time"},  # noisy stairwell, provisional
}

# --- Camere silenziose / night heat-guard (#2b) ------------------------------
# Bedrooms = ONLY main_bedroom (Padronale) + gabriroom (Gabriele); flagged
# `bedroom` in ZONES with their `manuale_switch`. (Camera Ospiti is now Studio V,
# an office -> NOT a bedroom.) Entering Notte silences them (manuale on + fan
# off); a heat-guard runs the fan at a low stage if the room overheats, then
# silences again once it cools; leaving Notte (or auto-wake) restores AUTO.
NIGHT_GUARD_HIGH = timedelta(minutes=3)   # above threshold this long -> low cooling
NIGHT_GUARD_LOW = timedelta(minutes=10)   # below threshold this long -> silence
NIGHT_GUARD_FAN_PCT = 33                  # lowest fancoil stage

# Options-flow tunables (entry.options) + defaults.
OPT_NIGHT_THRESHOLD = "night_heat_threshold"
OPT_AUTO_WAKE_TIME = "auto_wake_time"
DEFAULT_NIGHT_THRESHOLD = 26.0            # °C (was input_number.soglia_caldo_notte)
DEFAULT_AUTO_WAKE_TIME = "08:00:00"

# --- Away auto-escalation (#2c) ----------------------------------------------
# After the adults are away this long (while in Casa/Notte) -> Via; when they
# return and the house is in the auto-set Via, restore Casa. Replaces the legacy
# automation.clima_backup_via_quando_esco.
PRESENCE_GROUP = "group.presenza_adulti"  # person.mattia_pontacolone + person.ehi
OPT_AWAY_HOURS = "away_escalation_hours"
DEFAULT_AWAY_HOURS = 18

# --- #5 Outdoor free-cooling shutoff -----------------------------------------
# Summer: when it's cool enough outside, suppress the fancoils (force
# building_protection) and let the house coast — fewer/shorter compressor runs.
# (Winter "free heating from sun" is a separate concern, tied to #6/#7.)
OPT_FREE_COOL_ENABLED = "free_cool_enabled"
OPT_FREE_COOL_OUTDOOR = "free_cool_outdoor"
DEFAULT_FREE_COOL_ENABLED = True
DEFAULT_FREE_COOL_OUTDOOR = 22.0  # °C: outdoor below this -> no active cooling

# --- #6 Solar shading --------------------------------------------------------
# Summer: close a sun-facing shutter when the sun is on its facade and it's
# bright, to cut the solar gain on the cooled rooms (the proven lever for the
# gain-limited rooms). Cover -> zone/orientation/floor is resolved at runtime
# from the registries (device label = orientation), not hardcoded.
OPT_SHADING_ENABLED = "shading_enabled"
OPT_SHADING_SOLAR = "shading_solar_threshold"
OPT_SHADING_DEFAULT_POSITION = "shading_default_position"
DEFAULT_SHADING_ENABLED = True
DEFAULT_SHADING_SOLAR = 200.0   # W/m²: close sun-facing covers above this
# Per-room shade target uses the native HA cover position (0 = fully closed/down,
# 100 = fully open). Shading drives each sun-facing room's blind to this position
# instead of slamming it fully shut. The global default is the gentler fallback
# for rooms whose per-room number isn't tuned; each room exposes a
# `number.*_shade_position` (override) + a `switch.*_shade_block` (skip) entity.
DEFAULT_SHADING_POSITION = 50   # HA position: half-down by default (gentle)
SHADE_POSITION_MIN = 0
SHADE_POSITION_MAX = 100
SHADE_POSITION_STEP = 5
SHADE_POSITION_TOLERANCE = 4.0  # ±position counts as "there" (covers don't land exact)
SHADING_MIN_ELEVATION = 5.0     # deg: sun must be this far above the horizon
SHADING_ORIENTATIONS = ("north", "east", "south", "west")
# Compass azimuth band per facade (deg); north wraps through 0/360.
SHADING_AZIMUTH_BANDS: dict[str, tuple[float, float]] = {
    "north": (315.0, 45.0),
    "east": (45.0, 135.0),
    "south": (135.0, 225.0),
    "west": (225.0, 315.0),
}
# Area ids that mean "unassigned" -> skip the cover (e.g. the orphan tapparella).
SHADING_SKIP_AREAS = ("da_trovare",)

# --- #9 Central duty-cycle (max stint + cooloff via the Consenso BLOCCO) ------
# Cap the villa's continuous cooling stint; when it's exceeded, force a cooloff
# (BLOCCO block) for a fixed period, then release. Opt-in via switch.duty_cycle.
# A zone above the comfort-max aborts/prevents the cooloff (comfort wins).
OPT_DUTY_MAX_STINT = "duty_max_stint_min"   # minutes of continuous cooling
OPT_DUTY_COOLOFF = "duty_cooloff_min"       # minutes of forced rest
OPT_DUTY_COMFORT_MAX = "duty_comfort_max"   # °C: abort cooloff above this
OPT_DUTY_PEAK_OUTDOOR = "duty_peak_outdoor"  # °C: at/above this -> no duty (peak)
DEFAULT_DUTY_MAX_STINT = 120
DEFAULT_DUTY_COOLOFF = 30
DEFAULT_DUTY_COMFORT_MAX = 27.0
DEFAULT_DUTY_PEAK_OUTDOOR = 30.0  # duty-adaptive: above this, let the PdC run

# --- #3 v2: comfort-band control + capacity-matched fan (F1) ------------------
# The KNX thermostat's internal hysteresis is too narrow -> the valve bang-bangs
# every ~2 min near setpoint. We impose our OWN wide hysteresis by slamming the
# setpoint: RUN drives setpoint to target-A (valve forced open) until the room
# reaches target-B/2; REST drives it to target+A (valve closed) until target+B/2.
# Long, uniform cycles instead of chatter. Within a RUN the fan is sized to the
# thermal load (capacity-matched), so it's quiet where less power is needed.
# Opt-in via switch.fan_pacing. Salotto+cucina (one open space) move together.
OPT_BAND_WIDTH = "band_width"      # B: total comfort band (°C) the room swings in
OPT_BAND_SLAM = "band_slam"        # A: setpoint slam amplitude (°C); default B/2
OPT_FAN_MIN = "fan_min"            # global rest/min-circulation fan % (0 -> off)
DEFAULT_BAND_WIDTH = 1.5
DEFAULT_BAND_SLAM = 0.75           # = B/2
DEFAULT_FAN_MIN = 0                # off during REST unless raised (per-area override)
FAN_LEVEL_STEP = 10               # quantize the fan to 10 levels (each 10%)
FAN_LEVEL_HYSTERESIS = 5          # % dead-zone to avoid hunting between levels

# Prior per-room thermal model (capacity-matched fan). PLACEHOLDERS: F2 replaces
# these with values learned online per room. Comfort is guaranteed by the band
# regardless of fan accuracy, so rough priors are safe (they only affect how
# quiet / how fast a run is). dT/dt = a(T_out-T) + b*S + c - k*u.
COOL_CAPACITY = 1.2          # k: °C/h of cooling at 100% fan (measured best ~0.85)
COOL_GAIN_OUTDOOR = 0.03     # a: °C/h per °C of (T_out - T_in)
COOL_GAIN_SOLAR = 0.0008     # b: °C/h per W/m² of solar radiation
COOL_GAIN_BASE = 0.0         # c: baseline internal-gain °C/h
COOL_PULLDOWN = 0.3          # r: target pull-down rate during a RUN (°C/h)

# --- F2: online self-refining per-room thermal model (RLS) --------------------
# Learn dT/dt = a(T_out-T) + b*S + c - k*u_eff per room from live data.
# {a,b,c} are learned on w=False windows (no chilled water -> the -k*u term is 0);
# k is learned on w=True + fan-held-by-pacing windows (F2b). The estimator is an
# OBSERVER (never actuates) and runs even deploy-dark, so passive params converge
# before actuation lights up. Learned params reach control via blend_params
# (prior -> learned by confidence), so behaviour == F1 until a room converges.
OPT_MODEL_ENABLED = "model_learning_enabled"
DEFAULT_MODEL_ENABLED = True       # the observer is read-only; safe on by default
MODEL_FORGETTING = 0.995           # RLS forgetting (slow, below control bandwidth)
MODEL_RATE_WINDOW_MIN = 15.0       # min span (min) to estimate dT/dt (vs 0.1°C/30s noise)
MODEL_RATE_MAX_MIN = 45.0          # cap the rate window (track slowly-varying conditions)
MODEL_W_EDGE_SKIP = 3              # cycles to skip after a chilled-water edge (KNX off-delay)
# Physical bounds (project every update; reject NaN/inf; clamp k>0 so capacity_fan
# never sees a sign-flipping negative k):
MODEL_MAX_A = 0.5
MODEL_MAX_B = 0.01
MODEL_MAX_C = 3.0
MODEL_MIN_K = 0.1
MODEL_MAX_K = 5.0
# Initial RLS covariance (weak prior: lets data move the params, bounded so a bad
# first sample can't explode them). Passive diag for (a, b, c); scalar for k.
MODEL_P0_PASSIVE = (0.5, 1e-5, 4.0)
MODEL_P0_K = 4.0
# Confidence handover: updates before a learned coefficient is fully trusted by
# control (smooth blend weight = n / (n + conf_min)).
MODEL_ABC_CONF_MIN = 40
MODEL_K_CONF_MIN = 20
# k is learned only on a HELD, STEADY fan window (manuale on + the % barely moving),
# never from AUTO/unknown or a pull-down transient.
MODEL_CAP_FAN_STABILITY = 12   # max (max-min) fan % spread over the window to learn k

# --- #9 forecast run-window planner (pre-cool) -------------------------------
# Feed-forward on the hourly weather forecast: if a hot peak is coming within the
# lead window, "pre-cool" — don't let the duty cycle rest (bank coolth) and nudge
# the fancoil setpoints colder so the house enters the peak already cold.
WEATHER_ENTITY_DEFAULT = "weather.forecast_home"
OPT_WEATHER_ENTITY = "weather_entity"
# High thermal mass needs a LONG lookahead: see the peak hours ahead and bank
# coolth in the cool morning. Re-planned every FORECAST_REFRESH (30 min).
OPT_PRECOOL_LOOKAHEAD_HOURS = "precool_lookahead_hours"
OPT_PRECOOL_MARGIN = "precool_margin"
OPT_PRECOOL_OFFSET = "precool_offset"
DEFAULT_PRECOOL_LOOKAHEAD_HOURS = 12
# Pre-cool only while it's at least this much cooler now than the coming peak —
# so a 12 h window doesn't pre-cool all day; it tapers as the peak nears (then
# peak-skip takes over).
DEFAULT_PRECOOL_MARGIN = 3.0
DEFAULT_PRECOOL_OFFSET = 1.5     # °C below the normal target while pre-cooling
FORECAST_REFRESH = timedelta(minutes=30)  # re-fetch + re-plan cadence

# --- Window pause (#4) -------------------------------------------------------
# An open window/vasistas in a zone pauses that zone's cooling (building_protection)
# until it closes. Per-zone opening entity is the `window` key in ZONES (only
# gabriroom wired today; add more `window` entries as contact sensors are fitted).
# Openings can be a cover (vasistas) or a binary_sensor (contact).
WINDOW_OPEN_DELAY = timedelta(minutes=1)  # debounce: open this long before pausing
WINDOW_OPEN_STATES = ("open", "opening", "on")
WINDOW_CLOSED_STATES = ("closed", "off")
