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
# DATE carries the #8 return-home date.
PLATFORMS: list[Platform] = [
    Platform.DATE,
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
# Neutral preset the fail-safe (B1) hands zones back to on unload/removal — the
# thermostat resumes its own native KNX schedule (local autonomy), so no zone is
# ever left stuck in building_protection with no supervisor alive.
PRESET_AUTO = "auto"

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
# Corroborating season signal (robust Estate/Inverno, blessed in CLAUDE.md), used
# when the reference thermostat's hvac mode is inconclusive so a single
# unavailable KNX climate can't flip the organism into summer mid-winter.
SEASON_STAGIONE_SENSOR = "sensor.s5a_stagione"
SEASON_STAGIONE_SUMMER = "Estate"
SEASON_STAGIONE_WINTER = "Inverno"

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

# #2 per-room comfort offset: °C added to the house base center for a cooling
# zone (number.*_setpoint_offset). Negative = this room runs cooler than the
# house; the single slider still moves the whole house, rooms trim relative to it.
# The offset stacks on the season/mode offset, so it survives mode changes.
SETPOINT_OFFSET_MIN = -3.0
SETPOINT_OFFSET_MAX = 3.0
SETPOINT_OFFSET_STEP = 0.5
DEFAULT_SETPOINT_OFFSET = 0.0

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

# The fancoil fan entities (Phase-0 diagnostic speeds). NB: fan % is NOT the
# demand signal — true per-zone cooling demand is the EV valve state
# (binary_sensor.fancoil_*_valvola, ETS-verified 2026-06-24); an OFF fan reads
# as 0% regardless of the retained bus % (see coordinator._fan_pct).
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
        "climate": "climate.palestra_termostato_2",     # radiant thermostat (HEAT ONLY)
        "temp_sensor": "sensor.clima_palestra",
        "split_climate": "climate.aircon_palestra_2",   # split AC (trio) — the cooler
        "ac_group": "split_trio",
        # EP 3febdc verified live 2026-07-08 (temp + occupancy available) — used by
        # #6 split occupancy cooling. Was stale-None in const.
        "ep_temp": "sensor.everything_presence_one_3febdc_temperature",
        "ep_occ": "binary_sensor.everything_presence_one_3febdc_occupancy",
        "split_role": "comfort",   # #6: summer occupancy cool, off in winter/away
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
        # RH for #6 humidity handling (the split has NO humidity attr). EP a8c934
        # channel — verified live 2026-07-09 (44%); flaps to unavailable, so the
        # control law falls back to temp-only when it's stale.
        "humidity_sensor": "sensor.everything_presence_one_a8c934_humidity",
        "split_role": "storage",   # #6: wine — self-regulating cool @ setpoint, priority
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
        "split_role": "manual",    # #6: owner-triggered only — observe, never commanded
        "emitter": "split_ac",
    },
}

# 3-stage fancoil speeds observed (KNX): low/med/high
FAN_STAGES = (33, 67, 100)

# A temperature source older than this is treated as stale -> fall back (#1).
TEMP_STALE_AFTER = timedelta(minutes=30)

# B4 diagnostic: a controlled cooling leader whose fused temp is None for this many
# consecutive engine cycles (≈5 min at 30 s) has silently dropped out of band
# control — the engine logs a WARNING once and surfaces it on sensor.hvac_plan.
STALE_TEMP_CYCLES = 10

# Watchdog (proven live 2026-07-02/03): a fan commanded >0% that stays OFF for
# this many consecutive actuating cycles (≈5 min at 30 s) means the zone is NOT
# cooling — the KNX fancoil controller holds the EV valve closed while the fan
# is off — even though the band thinks it is RUNning. WARN once per fan.
RUN_FAN_OFF_WARN_CYCLES = 10

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

# #2b clock-derived wake: within [wake_time, wake_time + this) the night silence
# is lifted even if the in-memory wake latch was lost to a reboot/reload in
# Notte (a restart after 08:00 must NOT re-silence the bedrooms until the mode
# leaves Notte). 12 h = the "day"; an early-evening Notte re-silences normally.
NIGHT_WAKE_DAY_MINUTES = 720

# Options-flow tunables (entry.options) + defaults.
OPT_NIGHT_THRESHOLD = "night_heat_threshold"
OPT_AUTO_WAKE_TIME = "auto_wake_time"
DEFAULT_NIGHT_THRESHOLD = 26.0            # °C (was input_number.soglia_caldo_notte)
DEFAULT_AUTO_WAKE_TIME = "08:00:00"

# --- Away auto-escalation (#2c) ----------------------------------------------
# After the adults are away this long (while in Casa/Notte) -> Via; when they
# return and the house is in the auto-set Via, restore Casa. Replaces the legacy
# automation.clima_backup_via_quando_esco.
# Durable presence source (#7): watch the adult `person.*` entities DIRECTLY.
# The old `group.presenza_adulti` was a volatile `group.set` group that vanished
# on every HA restart (silently killing #2c until a boot-recreate automation ran);
# person entities survive restarts. Aggregate = home if ANY adult is home,
# not_home if all known adults are away, None if all unknown/unavailable.
PRESENCE_PERSONS = ("person.mattia_pontacolone", "person.ehi")
# Legacy — superseded by PRESENCE_PERSONS. Kept for reference only; the boot-recreate
# automation.sistema_ricrea_group_presenza_adulti_all_avvio can now be retired.
PRESENCE_GROUP = "group.presenza_adulti"
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
# Proportional shading (#6 enhancement): scale the shade DEPTH by how bright it is
# (+ a hot-outdoor boost) instead of a flat position — gentle at the trigger
# threshold, ramping to the configured default (the deepest shade) as irradiance
# approaches SHADING_PROP_SOLAR_FULL. Opt-in; a per-room number still hard-overrides.
OPT_SHADING_PROPORTIONAL = "shading_proportional"
DEFAULT_SHADING_PROPORTIONAL = False
SHADING_PROP_SOLAR_FULL = 700.0   # W/m² at/above -> deepest shade (the default pos)
SHADING_PROP_TEMP_REF = 28.0      # °C where the outdoor-heat boost starts
SHADING_PROP_TEMP_FULL = 38.0     # °C where the outdoor-heat boost saturates
SHADING_PROP_TEMP_WEIGHT = 0.35   # how much a hot day can deepen the shade
SHADE_POSITION_MIN = 0
SHADE_POSITION_MAX = 100
SHADE_POSITION_STEP = 5
SHADE_POSITION_TOLERANCE = 4.0  # ±position counts as "there" (covers don't land exact)
SHADING_MIN_ELEVATION = 5.0     # deg: sun must be this far above the horizon
SHADING_ORIENTATIONS = ("north", "east", "south", "west")
# Compass azimuth band per facade (deg); north wraps through 0/360.
# NB the villa is rotated ~45°: the "south" label = the real SW facade (~225°),
# "west" = WNW (~292°). The naive south band (135, 225) RELEASED exactly as the
# afternoon sun peaked on the SW glass (proven live 3/7: studio_v solar-loaded
# to 27.6 °C) — widened to (135, 270) so SW stays covered; west (225, 315)
# already covers WNW, and the 225-270 overlap is harmless (a cover matches only
# its own label's band).
SHADING_AZIMUTH_BANDS: dict[str, tuple[float, float]] = {
    "north": (315.0, 45.0),
    "east": (45.0, 135.0),
    "south": (135.0, 270.0),
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

# --- Comfort FLOOR (F4c Phase 1) ---------------------------------------------
# A lower bound on the band center, symmetric to duty_comfort_max (the ceiling):
# no center-LOWERING feature (#9 pre-cool, PV bank) may drive a cooling zone's
# center below it — preventing over-pre-cool (cold occupied rooms + wasted
# energy). First-class + a prerequisite for the unified planner (which can drive
# the center down). Owner decision: an explicit option, default = house_setpoint −
# COMFORT_FLOOR_OFFSET when unset, clamped to a sane absolute range. It bounds only
# LOWERING; it never raises a legitimately-high Via/Notte setback center.
OPT_COMFORT_FLOOR = "comfort_floor"
COMFORT_FLOOR_OFFSET = 2.0     # dynamic default = house_setpoint − this
DEFAULT_COMFORT_FLOOR = 22.0   # options-flow default (= the default setpoint 24 − 2)
COMFORT_FLOOR_MIN = 16.0       # clamp: never bank below this
COMFORT_FLOOR_MAX = 26.0       # clamp: a floor above this would fight normal cooling

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
COOL_PULLDOWN = 0.3          # r: base pull-down rate during a RUN (°C/h)
# Stored-heat extraction horizon: a RUN fan is sized to also remove the room's
# excess over the band center within this many hours (2026-07-04 sizing law —
# the constant rate alone read ~0 demand when outdoor < room; see
# supervisor/control_law.effective_pulldown).
COOL_PULLDOWN_HOURS = 2.0
# A RUN fan may never be sized to 0%: the KNX fancoil interlock holds the EV
# valve closed while the fan is off, so a 0% RUN cools nothing by construction.
COOL_RUN_FAN_FLOOR = 20

# --- S_eff: per-facade effective solar input (STORY_SEFF) ---------------------
# Replace the model's solar regressor input b·S_ghi with b·S_eff, a per-zone
# effective irradiance COMPUTED from sun geometry + facade normals (cover
# labels) + live cover position. The physics constants live in
# supervisor/solar.py (pure). SupervisorConfig ANDs the option with
# SEFF_CONSUMERS_READY so a half-migrated tree can never run S_eff: the
# constant flipped True in the release that completed the §6 consumer table
# (estimator + trio/fold + house_load_index + planner horizon + PV + sensor).
# The option itself stays opt-in (default OFF) until live validation
# (STORY_SEFF §8 gates) passes.
OPT_SEFF_ENABLED = "seff_enabled"
DEFAULT_SEFF_ENABLED = False
SEFF_CONSUMERS_READY = True

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
# D1 identifiability gate (F4c Phase 4): {a,b,c} is only trustworthy for the planner
# once its passive (w=False) windows actually EXCITED the solar coefficient b — i.e.
# a window with real irradiance was fed, not only sunless nights. Track the max
# window-mean solar over passive windows (ThermalParams.s_hi) and require it >= this.
# NOTE: this gates the (later-phase) PLANNER eligibility only; it does NOT change the
# blend that feeds live fan sizing (that stays prior-dominant + band-guaranteed).
# Hard (gain-limited) rooms rarely produce w=True held-fan windows, so their learned
# k is a NIGHT-CALIBRATED LOWER BOUND — their planner trajectories stay ADVISORY
# until k converges (may never, at the ~0-net 34°C peak). See ENGINE_REVIEW §6.
MODEL_SOLAR_EXCITATION_MIN = 150.0   # W/m²: min max-window-solar for abc to be "identified"
# Estimator gap guard (STORY_SEFF §3): a learning window may never bridge an
# unobserved interval (skipped samples can hide a chilled-water stint inside a
# "passive" window). Singles / the ~40 s KNX blips pass; longer gaps restart.
MODEL_GAP_MAX_S = 180.0

# --- F3: regime selector + coalescing -----------------------------------------
# Aggregate per-room load/capacity -> regime; in MEDIUM, coalesce demand into
# shared run/rest windows. Ratio path trusted only once k has converged.
OPT_REGIME_ENABLED = "regime_enabled"        # opt-in to coalescing ACTUATION (F3c)
OPT_REGIME_PEAK_RATIO = "regime_peak_ratio"
OPT_REGIME_MEDIUM_RATIO = "regime_medium_ratio"
OPT_MIN_COMPRESSOR_ON = "min_compressor_on"
OPT_MIN_COMPRESSOR_OFF = "min_compressor_off"
DEFAULT_REGIME_ENABLED = False
DEFAULT_REGIME_PEAK_RATIO = 0.85     # g/k at/above this -> PEAK (no coalescing)
DEFAULT_REGIME_MEDIUM_RATIO = 0.10   # above this (and below peak) -> MEDIUM coalesce
DEFAULT_MIN_COMPRESSOR_ON = 10       # minutes: anti-short-cycle floor (guardrail)
DEFAULT_MIN_COMPRESSOR_OFF = 10
REGIME_K_CONF_MIN = 0.5              # per-zone k confidence to count toward the ratio
# F3b 12h per-room forward sim:
OPT_PRECOOL_MAX_DEPTH = "precool_max_depth"
DEFAULT_PRECOOL_MAX_DEPTH = 3.0      # °C: deepest pre-cool the planner will schedule
PLAN_SIM_STEP_MIN = 15              # forward-Euler macro step (sub-stepped internally)
PLAN_SIM_DOWNSAMPLE_MIN = 60       # store ~hourly points on the sensor

# --- F4a: solar forecast ------------------------------------------------------
# gw3000a gives only CURRENT solar; the 12h sim needs a curve. Estimate horizontal
# GHI (W/m², matching the gw3000a pyranometer) = clear_sky_ghi * sin(elevation) *
# (1 - cloud_fraction), from sun elevation (astral) × forecast cloud cover.
OPT_SOLAR_FORECAST = "solar_forecast_enabled"
DEFAULT_SOLAR_FORECAST = False      # opt-in until validated vs gw3000a on clear days
CLEAR_SKY_GHI = 1000.0            # W/m² peak clear-sky horizontal GHI (gw3000a hit
#                                   1044 near noon; only matters when NOT nowcast-
#                                   anchored, since the anchor makes it cancel out)
# F4a-v2: nowcast-anchor the solar curve to the live gw3000a; fall back to the
# Forecast.Solar PV forecast when the pyranometer is missing. Validated 2026-07-01:
# Met.no cloud is unreliable here (said rainy at full sun), Forecast.Solar tracks
# the daily shape but mis-scales day-to-day -> gw3000a is the trusted anchor.
FORECASTSOLAR_POWER = "sensor.power_production_now"  # Forecast.Solar est. PV power (W)
# Rough W(PV) -> W/m²(GHI) factor from 3-day history (gw3000a / forecast power),
# only used as a fallback anchor when the pyranometer reading is unavailable.
FORECASTSOLAR_GHI_FACTOR = 0.18

# --- F4b: per-room/per-fascia comfort windows --------------------------------
# Outside its comfort window a room may DRIFT warm (raise the band center by
# RELAX, quieter/efficient) — a setpoint MODIFIER only, capped so it NEVER goes
# above duty_comfort_max and NEVER suppresses a real comfort breach (the band
# still cools above the relaxed center). Bedrooms use the night window, day rooms
# the day window — matching the owner's "bedrooms 22-08, living areas 08-23".
OPT_COMFORT_ENABLED = "comfort_windows_enabled"
OPT_COMFORT_RELAX = "comfort_relax"
OPT_COMFORT_DAY_FROM = "comfort_day_from"
OPT_COMFORT_DAY_TO = "comfort_day_to"
OPT_COMFORT_NIGHT_FROM = "comfort_night_from"
OPT_COMFORT_NIGHT_TO = "comfort_night_to"
DEFAULT_COMFORT_ENABLED = False
DEFAULT_COMFORT_RELAX = 2.0          # °C the center rises outside the window (capped)
DEFAULT_COMFORT_DAY_FROM = "08:00"
DEFAULT_COMFORT_DAY_TO = "23:00"
DEFAULT_COMFORT_NIGHT_FROM = "22:00"
DEFAULT_COMFORT_NIGHT_TO = "08:00"
# Coalescing band hysteresis (separate enter/exit so house RUN/REST doesn't flap):
COALESCE_ENTER_FRACTION = 0.5        # enter RUN at center + ENTER_FRACTION*B/2 above
COALESCE_EXIT_FRACTION = 0.5         # exit REST only when leader <= center - EXIT*B/2

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

# --- F4c Phase 6: unified planner DRIVES the band center (opt-in) -------------
# The reference is a SLOW-moving schedule: recomputed at the forecast cadence (or
# on a mode change), read forward via CenterSchedule.at(zone, now) by the fast
# 30 s reactive loop. If the last good schedule is older than this (e.g. the
# forecast fetch has been failing), the band controller falls back to the base
# ladder — never drives off a stale 12 h reference sized to a peak that has moved.
SCHEDULE_MAX_AGE = timedelta(minutes=90)

# --- Split-AC trio (#6) ------------------------------------------------------
# The 3 Daikin heads (Palestra/Cantina/Garage) on ONE shared outdoor heat pump,
# bridged by Zennio KLIC-DD gateways. Separate refrigerant circuit from the PdC.
# Single direction only: cool/dry compatible, heat exclusive — the controller
# emits cool-side only, so it can never create a mode conflict. Opt-in
# switch.split_ac (deploy-dark, default off). Automated scope = Cantina (wine
# storage) + Palestra (summer occupancy cool); Garage is owner-manual
# (observe-only, never commanded). See STORY_SPLIT_TRIO.md.
SPLIT_GROUP = "split_trio"                        # ac_group key in ZONES
OPT_SPLIT_ENABLED = "split_ac_enabled"            # mirrored by switch.split_ac
OPT_SPLIT_CANTINA_SETPOINT = "split_cantina_setpoint"
OPT_SPLIT_PALESTRA_SETPOINT = "split_palestra_setpoint"
DEFAULT_SPLIT_ENABLED = False                     # strict deploy-dark
DEFAULT_SPLIT_CANTINA_SETPOINT = 19.0             # °C, owner-set wine storage target
DEFAULT_SPLIT_PALESTRA_SETPOINT = 24.0            # °C, gym comfort target (its own, not the house slider)
# Group compressor protection (industry-standard defaults, minutes): we are the
# only gate on this heat pump. Setpoint hysteresis is preferred over on/off, so
# start events stay rare regardless.
OPT_SPLIT_MIN_ON = "split_min_on"
OPT_SPLIT_MIN_OFF = "split_min_off"
OPT_SPLIT_MODE_LOCKOUT = "split_mode_lockout"
DEFAULT_SPLIT_MIN_ON = 5
DEFAULT_SPLIT_MIN_OFF = 3
DEFAULT_SPLIT_MODE_LOCKOUT = 10
# Cantina humidity band (wine). A split can only DEHUMIDIFY: run `dry` above the
# ceiling; below the floor relax the setpoint so it dries the cellar less. A cellar
# that trends below the floor needs a HUMIDIFIER (out of scope) — the AC can't add
# moisture. RH ceiling/floor in %.
OPT_SPLIT_RH_CEILING = "split_rh_ceiling"
OPT_SPLIT_RH_FLOOR = "split_rh_floor"
DEFAULT_SPLIT_RH_CEILING = 65.0
DEFAULT_SPLIT_RH_FLOOR = 55.0

# --- Window pause (#4) -------------------------------------------------------
# An open window/vasistas in a zone pauses that zone's cooling (building_protection)
# until it closes. Per-zone opening entity is the `window` key in ZONES (only
# gabriroom wired today; add more `window` entries as contact sensors are fitted).
# Openings can be a cover (vasistas) or a binary_sensor (contact).
WINDOW_OPEN_DELAY = timedelta(minutes=1)  # debounce: open this long before pausing
WINDOW_OPEN_STATES = ("open", "opening", "on")
WINDOW_CLOSED_STATES = ("closed", "off")

# --- PV/energy-aware daily pre-cool (F4c-lite) -------------------------------
# Bank coolth at the thermodynamically most effective hours (cool + low solar gain),
# using the solar forecast + battery as a buffer, so the hot/expensive evening needs
# minimal compressor. Works PURELY through the band center (requires fan_pacing) — it
# adds no new lever, so it stays clear of the BLOCCO/duty fail-safe cluster. Opt-in
# via switch.pv_bias (deploy-dark). See STORY_PV_BIAS.md + memory condominio-pv-energy-map.
# Condominio FusionSolar entities (node ne=199688300; the _2 suffix is CONDOMINIO for
# these FusionSolar-named entities — verified live 2026-07-01).
PDC_LOAD_POWER = "sensor.shellypro3em63_e08cfe9573ac_power"  # W: PdC + pumps (local clamp)
CONDOMINIO_BATTERY_SOC = "sensor.battery_percentage_2"       # %
CONDOMINIO_BATTERY_POWER = (
    "sensor.energy_battery_battery_consumption_power_2_battery_injection_power_2_net_power"
)  # W, NEGATIVE = charging, POSITIVE = discharging
CONDOMINIO_GRID_POWER = (
    "sensor.energy_grid_grid_consumption_power_2_grid_injection_power_2_net_power"
)  # W, POSITIVE = import, NEGATIVE = export (export ~never; self-consumption ~100%)
CONDOMINIO_PV_REMAINING = "sensor.fusion_solar_condominio_panel_production_remaining_today"  # kWh

OPT_PV_BIAS_FLOOR_RICH = "pv_floor_rich"     # bank band-center floor when solar-rich (°C)
OPT_PV_BIAS_FLOOR_POOR = "pv_floor_poor"     # bank band-center floor when solar-poor (°C)
OPT_PV_BIAS_COAST_RELAX = "pv_coast_relax"   # °C to raise center when coasting (capped)
OPT_PV_BIAS_EFF_FRACTION = "pv_eff_fraction"  # bank when eff_now >= frac * eff_peak
OPT_PV_BIAS_EFF_MIN = "pv_eff_min"           # °C/h below which an hour is "ineffective"
OPT_PV_BIAS_DAILY_NEED_KWH = "pv_daily_need_kwh"  # est. daily Condominio consumption
# Defaults are SUMMER cooling values; they must be revised for the heating season.
DEFAULT_PV_BIAS_FLOOR_RICH = 22.0
DEFAULT_PV_BIAS_FLOOR_POOR = 23.0
DEFAULT_PV_BIAS_COAST_RELAX = 1.5
DEFAULT_PV_BIAS_EFF_FRACTION = 0.6
DEFAULT_PV_BIAS_EFF_MIN = 0.1
DEFAULT_PV_BIAS_DAILY_NEED_KWH = 35.0
# Min-dwell: hold the bank/coast/hold decision this long before flipping, so a mode
# change (which jumps the band center ~2°C >> band) can't slam the valve cycle-to-cycle.
PV_BIAS_MIN_DWELL = timedelta(minutes=20)

# --- Story #8: return-home pre-conditioning ----------------------------------
# While away (house_mode Via) with a return ETA armed, the house sits in deep
# setback (building_protection) and starts pre-conditioning `lead_time` before the
# ETA. Coarse ETA = a date + a daypart mapped to a canonical hour. #8 overrides the
# EFFECTIVE house mode (Vacanza while waiting, Casa during pre-cond) so the whole
# existing stack follows. Opt-in via switch.villa_hvac_return_precond (deploy-dark).
RETURN_DAYPART_MORNING = "mattino"
RETURN_DAYPART_AFTERNOON = "pomeriggio"
RETURN_DAYPART_EVENING = "sera"
RETURN_DAYPARTS: list[str] = [
    RETURN_DAYPART_MORNING,
    RETURN_DAYPART_AFTERNOON,
    RETURN_DAYPART_EVENING,
]
# Canonical hour each daypart resolves to for the ETA / lead-time math.
DEFAULT_RETURN_DAYPART_HOURS: dict[str, int] = {
    RETURN_DAYPART_MORNING: 8,
    RETURN_DAYPART_AFTERNOON: 14,
    RETURN_DAYPART_EVENING: 19,
}
# Options-flow tunables.
OPT_RETURN_MAX_LEAD_HOURS = "return_max_lead_hours"  # clamp the pre-cond lead time
OPT_RETURN_MARGIN_MIN = "return_margin_min"          # safety margin on the lead time
OPT_NOTIFY_TARGET = "notify_target"                  # notify.* service for the ask
DEFAULT_RETURN_MAX_LEAD_HOURS = 6.0
DEFAULT_RETURN_MARGIN_MIN = 30.0
# Actionable-notification identifiers (the ask fired on entering Via).
RETURN_NOTIFY_TAG = "villa_hvac_return_home"
RETURN_ACTION_PREFIX = "VILLA_HVAC_RETURN_"  # + TODAY_EVENING / TOMORROW_MORNING / …
RETURN_ACTION_UNKNOWN = "VILLA_HVAC_RETURN_UNKNOWN"
