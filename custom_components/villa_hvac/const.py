"""Constants and zone map for the Villa HVAC orchestration integration.

Verified against the live Home Assistant on 2026-06-23.
The real PdC call signals are KNX binary_sensors (NOT climate hvac_action):
cooling consenso turns on when any fancoil fan > 0.
"""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "villa_hvac"

# Start with read-only diagnostics. Control platforms (switch/number) added later.
PLATFORMS: list[Platform] = [Platform.SENSOR]

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

# --- Zone map (verified 2026-06-23) ------------------------------------------
# The lever to stop a zone calling = set its KNX thermostat preset to
# 'building_protection' (validated: drives the fancoil fan to 0 -> consenso off).
# ep_temp / ep_occ entity_ids to be completed from each EP device where present.
ZONES: dict[str, dict] = {
    "living_room": {
        "name": "Salotto",
        "floor": "terra",
        "climate": "climate.salotto_termostato_2",
        "fancoils": ["fan.fancoil_salotto", "fan.fancoil_cucina"],  # kitchen follows salotto
        "ep_device": "EP Living Room",
        "emitter": "fancoil",  # summer
    },
    "kitchen": {
        "name": "Kitchen",
        "floor": "terra",
        "climate": None,  # open-space: follows Salotto
        "fancoils": ["fan.fancoil_cucina"],
        "ep_device": "EP Kitchen",
        "follows": "living_room",
    },
    "main_bedroom": {
        "name": "Camera Padronale",
        "floor": "secondo",
        "climate": "climate.camera_padronale_termostato_2",
        "fancoils": ["fan.fancoil_camera_padronale"],
        "ep_device": "EP Main Bedroom",
        "emitter": "fancoil",
    },
    "gabriroom": {
        "name": "Camera Gabriele",
        "floor": "secondo",
        "climate": "climate.camera_gabriele_termostato_2",
        "fancoils": ["fan.fancoil_camera_gabriele"],
        "ep_device": "EP GabriRoom",
        "emitter": "fancoil",
    },
    "studio_v": {
        "name": "Studio V",
        "floor": "secondo",
        "climate": "climate.studio_v_termostato_2",
        "fancoils": ["fan.fancoil_camera_ospiti"],
        "ep_device": "EP Studio V",
        "emitter": "fancoil",
    },
    "sala_giochi": {
        "name": "Sala Giochi",
        "floor": "primo",
        "climate": "climate.sala_giochi_termostato_2",
        "fancoils": ["fan.fancoil_sala_giochi"],
        "ep_device": "EP Sala Giochi",
        "emitter": "fancoil",
    },
    "office": {
        "name": "Office (Studio)",
        "floor": "primo",
        "climate": "climate.studio_termostato_2",
        "fancoils": ["fan.fancoil_studio_pianerottolo_p1"],
        "ep_device": "EP Office",
        "emitter": "fancoil",
    },
    "stairs_p1": {
        "name": "Pianerottolo P1",
        "floor": "primo",
        "climate": "climate.pianerottolo_p1_termostato_2",
        "fancoils": ["fan.fancoil_locale_rack"],  # rack fancoil also cools P1
        "ep_device": "EP Stairs P1",
        "emitter": "fancoil",
        "rack_temp": "sensor.rack_t_h_temperature",  # OR condition: rack protection
    },
    # Radiant-only / no fancoil zones (winter heating): bagni, lavanderia, ingresso,
    # pianerottolo P2, palestra (also split AC), cantina vini (split AC), garage (split AC).
    # Added once cooling-side MVP is proven.
}

# 3-stage fancoil speeds observed (KNX): low/med/high
FAN_STAGES = (33, 67, 100)
