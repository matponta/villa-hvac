"""v0.77.0 — winter light: house temperature through the thermostats (setpoint
only), per-room trim + Economy on every thermostat zone, gentle night."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import (
    DOMAIN,
    PRESET_BUILDING_PROTECTION,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
    WINTER_SETPOINT_MAX,
    WINTER_SETPOINT_MIN,
    ZONES,
)
from custom_components.villa_hvac.controller import mode_offset
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.policies import house_mode_policy
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    preset_lever,
    temperature_lever,
)

RADIANT = "bagno_gabriele"
FANCOIL = "main_bedroom"
N_THERMOSTATS = 17
_NOW = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


def _z(zid, emitter, *, offset=0.0, enabled=True, paused=False):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter=emitter,
        enabled=enabled, paused=paused, setpoint_offset=offset,
    )


def _winter(zones, mode="Casa", setpoint=21.0, offset=0.0):
    return HouseState(
        now=_NOW, zones={z.zone_id: z for z in zones}, season=SEASON_WINTER,
        house_mode=mode, auto_setback=True, house_setpoint=setpoint,
        mode_offset=offset,
    )


# --- pure policy ------------------------------------------------------------------

def test_winter_is_setpoint_only_comfort_preset_all_modes():
    zones = [_z("r", "radiant"), _z("f", "fancoil")]
    for mode, off, want in (("Casa", 0.0, 21.0), ("Via", -2.0, 19.0), ("Notte", -1.0, 20.0)):
        out = house_mode_policy(_winter(zones, mode=mode, offset=off))
        for zid in ("r", "f"):
            assert out[preset_lever(f"climate.{zid}")] == "comfort"
            assert out[temperature_lever(f"climate.{zid}")] == want


def test_winter_vacation_keeps_frost_protection_and_writes_no_temperature():
    out = house_mode_policy(_winter([_z("r", "radiant")], mode="Vacanza", offset=None))
    assert out == {preset_lever("climate.r"): PRESET_BUILDING_PROTECTION}


def test_winter_room_trim_and_economy_offset_stack_and_clamp():
    out = house_mode_policy(_winter([
        _z("warm", "radiant", offset=+2.0),
        _z("eco", "radiant", offset=-3.0),         # economy folded into the trim
        _z("low", "radiant", offset=-6.0 - 3.0),   # would be 12 -> clamped
        _z("hot", "radiant", offset=+9.0),         # would be 30 -> clamped
    ]))
    assert out[temperature_lever("climate.warm")] == 23.0
    assert out[temperature_lever("climate.eco")] == 18.0
    assert out[temperature_lever("climate.low")] == WINTER_SETPOINT_MIN
    assert out[temperature_lever("climate.hot")] == WINTER_SETPOINT_MAX


def test_winter_skips_disabled_and_paused_zones():
    out = house_mode_policy(_winter([
        _z("off", "fancoil", enabled=False), _z("win", "radiant", paused=True),
    ]))
    assert out == {}


def test_summer_path_unchanged():
    z = _z("f", "fancoil")
    s = HouseState(now=_NOW, zones={"f": z}, season=SEASON_SUMMER, house_mode="Via",
                   auto_setback=True, house_setpoint=24.0, mode_offset=5.0)
    out = house_mode_policy(s)
    assert out[preset_lever("climate.f")] == "standby"
    assert out[temperature_lever("climate.f")] == 29.0


# --- live wiring --------------------------------------------------------------------

async def _setup(hass, hvac="heat"):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, hvac, {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass, entry, domain, suffix):
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_{suffix}"
    )


async def test_entities_created_for_every_thermostat_zone(hass):
    entry = await _setup(hass)
    assert _eid(hass, entry, "number", "house_setpoint_winter")
    offsets = [z for z in ZONES if _eid(hass, entry, "number", f"setpoint_offset_{z}")]
    economy = [z for z in ZONES if _eid(hass, entry, "switch", f"{z}_economy")]
    assert len(offsets) == len(economy) == N_THERMOSTATS
    assert RADIANT in offsets and RADIANT in economy
    assert "cantina_vini" not in economy and "kitchen" not in economy


async def test_winter_state_reads_winter_slider_trim_and_economy(hass):
    entry = await _setup(hass)
    hass.states.async_set(_eid(hass, entry, "number", "house_setpoint"), "24.5")
    hass.states.async_set(_eid(hass, entry, "number", "house_setpoint_winter"), "21.5")
    hass.states.async_set(_eid(hass, entry, "number", f"setpoint_offset_{RADIANT}"), "1.0")
    hass.states.async_set(_eid(hass, entry, "switch", f"{FANCOIL}_economy"), "on")

    st = build_house_state(hass, entry, entry.runtime_data)
    assert st.season == SEASON_WINTER
    assert st.house_setpoint == 21.5                      # NOT the summer 24.5
    assert st.zones[RADIANT].setpoint_offset == 1.0        # radiant trim honoured
    assert st.zones[FANCOIL].setpoint_offset == -3.0       # Economy default
    out = house_mode_policy(st)
    assert out[temperature_lever(ZONES[FANCOIL]["climate"])] == 18.5
    assert out[temperature_lever(ZONES[RADIANT]["climate"])] == 22.5

    # Summer: the summer slider, no radiant trim, Economy inert.
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "cool", {"preset_mode": "comfort"})
    su = build_house_state(hass, entry, entry.runtime_data)
    assert su.house_setpoint == 24.5
    assert su.zones[RADIANT].setpoint_offset == 0.0
    assert su.zones[FANCOIL].setpoint_offset == 0.0


async def test_winter_night_default_is_gentle(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat")
    assert mode_offset(hass, entry, "Notte") == -1.0
    assert mode_offset(hass, entry, "Via") == -2.0
