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
PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

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

# Zone emitters whose KNX thermostat accepts the comfort/standby/economy ladder
# (the 17 *_termostato_2). Split-AC zones (aircon_*) are excluded from #2.
PRESET_CONTROLLABLE_EMITTERS = ("fancoil", "radiant")

# --- Real call signals (KNX) -------------------------------------------------
CONSENSO_FREDDO = "binary_sensor.ct_consenso_freddo_villa"  # cooling call to PdC
CONSENSO_CALDO = "binary_sensor.ct_consenso_caldo_villa"    # heating call to PdC

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
        "window": "cover.vasistas_gabriele",  # #4 window pause (only wired zone)
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
    },
    "bagno_gabriele": {
        "name": "Bagno Gabriele",
        "floor": "secondo",
        "climate": "climate.bagno_gabriele_termostato_2",
        "temp_sensor": "sensor.clima_bagno_gabriele",
        "ep_temp": None,
        "ep_occ": None,
        "emitter": "radiant",
    },
    "bagno_giochi": {
        "name": "Bagno Giochi",
        "floor": "primo",
        "climate": "climate.bagno_giochi_termostato_2",
        "temp_sensor": "sensor.clima_bagno_giochi",
        "ep_temp": "sensor.everything_presence_one_5a7d68_temperature",  # MEDIUM conf
        "ep_occ": "binary_sensor.everything_presence_one_5a7d68_occupancy",
        "emitter": "radiant",
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

# --- Window pause (#4) -------------------------------------------------------
# An open window/vasistas in a zone pauses that zone's cooling (building_protection)
# until it closes. Per-zone opening entity is the `window` key in ZONES (only
# gabriroom wired today; add more `window` entries as contact sensors are fitted).
# Openings can be a cover (vasistas) or a binary_sensor (contact).
WINDOW_OPEN_DELAY = timedelta(minutes=1)  # debounce: open this long before pausing
WINDOW_OPEN_STATES = ("open", "opening", "on")
WINDOW_CLOSED_STATES = ("closed", "off")
