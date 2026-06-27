"""Unit tests for the pure preset policies (Phase A3)."""
from __future__ import annotations

from datetime import datetime

from custom_components.villa_hvac.const import (
    PRESET_BUILDING_PROTECTION,
    SEASON_SUMMER,
)
from custom_components.villa_hvac.policies import (
    PRESET_POLICIES,
    _azimuth_in_band,
    disabled_zones_policy,
    free_cool_policy,
    house_mode_policy,
    shading_policy,
    window_pause_policy,
)
from custom_components.villa_hvac.supervisor import (
    CoverInfo,
    HouseState,
    ZoneSnapshot,
    cover_lever,
    merge_desired,
    preset_lever,
    temperature_lever,
)

T0 = datetime(2026, 6, 27, 12, 0, 0)


def _zone(zid, climate="climate.x", emitter="fancoil", enabled=True, paused=False):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=climate, emitter=emitter,
        enabled=enabled, paused=paused,
    )


def _state(
    zones, *, mode="Casa", auto=True, setpoint=24.0, offset=0.0,
    season=None, outdoor=None, free_cool=False, free_cool_threshold=None,
    covers=(), azimuth=None, elevation=None, solar=None,
    shading=False, shading_solar=None,
):
    return HouseState(
        now=T0,
        zones={z.zone_id: z for z in zones},
        house_mode=mode,
        auto_setback=auto,
        house_setpoint=setpoint,
        mode_offset=offset,
        season=season,
        outdoor_temp=outdoor,
        free_cool_enabled=free_cool,
        free_cool_threshold=free_cool_threshold,
        covers=tuple(covers),
        sun_azimuth=azimuth,
        sun_elevation=elevation,
        solar=solar,
        shading_enabled=shading,
        shading_solar_threshold=shading_solar,
    )


# --- disabled (#10) ----------------------------------------------------------

def test_disabled_zone_forced_to_building_protection():
    z = _zone("a", climate="climate.a", enabled=False)
    out = disabled_zones_policy(_state([z]))
    assert out == {preset_lever("climate.a"): PRESET_BUILDING_PROTECTION}


def test_enabled_zone_not_touched_by_disabled_policy():
    assert disabled_zones_policy(_state([_zone("a", enabled=True)])) == {}


# --- window pause (#4) -------------------------------------------------------

def test_paused_zone_forced_to_building_protection():
    z = _zone("a", climate="climate.a", paused=True)
    out = window_pause_policy(_state([z]))
    assert out == {preset_lever("climate.a"): PRESET_BUILDING_PROTECTION}


def test_window_policy_respects_auto_setback_off():
    z = _zone("a", paused=True)
    assert window_pause_policy(_state([z], auto=False)) == {}


# --- house mode (#2a) --------------------------------------------------------

def test_house_mode_drives_preset_and_setpoint():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Casa", setpoint=24.0, offset=0.0))
    assert out[preset_lever("climate.a")] == "comfort"
    assert out[temperature_lever("climate.a")] == 24.0


def test_house_mode_applies_offset():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Via", setpoint=24.0, offset=5.0))
    assert out[preset_lever("climate.a")] == "standby"
    assert out[temperature_lever("climate.a")] == 29.0


def test_house_mode_skips_disabled_paused_and_noncontrollable():
    zones = [
        _zone("dis", climate="climate.dis", enabled=False),
        _zone("pause", climate="climate.pause", paused=True),
        _zone("split", climate="climate.split", emitter="split_ac"),
        _zone("nocl", climate=None),
        _zone("ok", climate="climate.ok"),
    ]
    out = house_mode_policy(_state(zones))
    assert preset_lever("climate.ok") in out
    for skipped in ("climate.dis", "climate.pause", "climate.split"):
        assert preset_lever(skipped) not in out


def test_house_mode_vacation_is_bp_with_no_setpoint():
    z = _zone("a", climate="climate.a")
    out = house_mode_policy(_state([z], mode="Vacanza", offset=None))
    assert out[preset_lever("climate.a")] == PRESET_BUILDING_PROTECTION
    assert temperature_lever("climate.a") not in out  # frost-fixed, no setpoint


def test_house_mode_noop_when_auto_setback_off_or_unknown_mode():
    z = _zone("a", climate="climate.a")
    assert house_mode_policy(_state([z], auto=False)) == {}
    assert house_mode_policy(_state([z], mode="???")) == {}


# --- free cooling (#5) -------------------------------------------------------

_FC = dict(season=SEASON_SUMMER, outdoor=20.0, free_cool=True, free_cool_threshold=22.0)


def test_free_cool_suppresses_fancoils_when_cool_outside():
    z = _zone("a", climate="climate.a", emitter="fancoil")
    out = free_cool_policy(_state([z], **_FC))
    assert out == {preset_lever("climate.a"): PRESET_BUILDING_PROTECTION}


def test_free_cool_noop_when_warm_disabled_winter_or_no_outdoor():
    z = _zone("a", climate="climate.a", emitter="fancoil")
    assert free_cool_policy(_state([z], season=SEASON_SUMMER, outdoor=26.0,
                                   free_cool=True, free_cool_threshold=22.0)) == {}
    assert free_cool_policy(_state([z], season=SEASON_SUMMER, outdoor=20.0,
                                   free_cool=False, free_cool_threshold=22.0)) == {}
    assert free_cool_policy(_state([z], season="winter", outdoor=20.0,
                                   free_cool=True, free_cool_threshold=22.0)) == {}
    assert free_cool_policy(_state([z], season=SEASON_SUMMER, outdoor=None,
                                   free_cool=True, free_cool_threshold=22.0)) == {}


def test_free_cool_only_fancoil_enabled_unpaused():
    zones = [
        _zone("fan", climate="climate.fan", emitter="fancoil"),
        _zone("rad", climate="climate.rad", emitter="radiant"),
        _zone("dis", climate="climate.dis", emitter="fancoil", enabled=False),
        _zone("pause", climate="climate.pause", emitter="fancoil", paused=True),
    ]
    out = free_cool_policy(_state(zones, **_FC))
    assert out == {preset_lever("climate.fan"): PRESET_BUILDING_PROTECTION}


def test_free_cool_overrides_house_mode_and_suppresses_setpoint():
    z = _zone("a", climate="climate.a", emitter="fancoil")
    state = _state([z], mode="Casa", offset=0.0, setpoint=24.0, **_FC)
    merged = merge_desired([p(state) for p in PRESET_POLICIES])
    assert merged[preset_lever("climate.a")] == PRESET_BUILDING_PROTECTION
    # suppressed -> house_mode skips it, so no setpoint is pushed onto the BP zone
    assert temperature_lever("climate.a") not in merged


# --- merged stack: priority disabled > window > house_mode -------------------

def test_merged_priority_overrides():
    zones = [
        _zone("dis", climate="climate.dis", enabled=False),
        _zone("pause", climate="climate.pause", paused=True),
        _zone("ok", climate="climate.ok"),
    ]
    state = _state(zones, mode="Casa", offset=0.0, setpoint=24.0)
    merged = merge_desired([p(state) for p in PRESET_POLICIES])
    assert merged[preset_lever("climate.dis")] == PRESET_BUILDING_PROTECTION
    assert merged[preset_lever("climate.pause")] == PRESET_BUILDING_PROTECTION
    assert merged[preset_lever("climate.ok")] == "comfort"
    # disabled/paused zones carry no setpoint (never push temp onto a BP zone)
    assert temperature_lever("climate.dis") not in merged
    assert temperature_lever("climate.pause") not in merged
    assert merged[temperature_lever("climate.ok")] == 24.0


# --- solar shading (#6) ------------------------------------------------------

def test_azimuth_in_band():
    assert _azimuth_in_band(180, "south") and not _azimuth_in_band(130, "south")
    assert _azimuth_in_band(270, "west") and not _azimuth_in_band(200, "west")
    assert _azimuth_in_band(90, "east")
    # north wraps through 0/360
    assert _azimuth_in_band(350, "north") and _azimuth_in_band(10, "north")
    assert not _azimuth_in_band(90, "north")


_SOUTH = CoverInfo(entity_id="cover.s", orientation="south")
_WEST = CoverInfo(entity_id="cover.w", orientation="west")
_SHADE = dict(
    season=SEASON_SUMMER, azimuth=180.0, elevation=30.0, solar=500.0,
    shading=True, shading_solar=200.0,
)


def test_shading_closes_only_the_sunlit_facade():
    out = shading_policy(_state([], covers=[_SOUTH, _WEST], **_SHADE))
    assert out == {cover_lever("cover.s"): "closed"}  # sun at 180 -> south only


def test_shading_noop_low_sun_low_solar_winter_or_disabled():
    assert shading_policy(_state([], covers=[_SOUTH],
                                 **{**_SHADE, "elevation": 2.0})) == {}
    assert shading_policy(_state([], covers=[_SOUTH],
                                 **{**_SHADE, "solar": 50.0})) == {}
    assert shading_policy(_state([], covers=[_SOUTH],
                                 **{**_SHADE, "season": "winter"})) == {}
    assert shading_policy(_state([], covers=[_SOUTH],
                                 **{**_SHADE, "shading": False})) == {}
    assert shading_policy(_state([], covers=[_SOUTH],
                                 **{**_SHADE, "azimuth": None})) == {}
