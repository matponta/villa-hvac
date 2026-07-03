"""Unit tests for the pure preset policies (Phase A3)."""
from __future__ import annotations

from datetime import datetime

from custom_components.villa_hvac.const import (
    PRESET_BUILDING_PROTECTION,
    SEASON_SUMMER,
)
from custom_components.villa_hvac.policies import (
    PRESET_POLICIES,
    FanBandController,
    _azimuth_in_band,
    disabled_zones_policy,
    free_cool_policy,
    house_mode_policy,
    precool_policy,
    proportional_shade_position,
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
    shading=False, shading_solar=None, shading_default=None,
    shading_proportional=False,
    consenso=None, night=False, fan_pacing=False,
    duty=False, precool=False, precool_offset=None,
):
    return HouseState(
        now=T0,
        zones={z.zone_id: z for z in zones},
        house_mode=mode,
        auto_setback=auto,
        house_setpoint=setpoint,
        mode_offset=offset,
        duty_enabled=duty,
        precool=precool,
        precool_offset=precool_offset,
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
        shading_default_position=shading_default,
        shading_proportional=shading_proportional,
        consenso_freddo=consenso,
        night_active=night,
        fan_pacing_enabled=fan_pacing,
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
    shading=True, shading_solar=200.0, shading_default=50,
)


def test_shading_drives_sunlit_facade_to_default_position():
    out = shading_policy(_state([], covers=[_SOUTH, _WEST], **_SHADE))
    assert out == {cover_lever("cover.s"): 50}  # sun at 180 -> south to default


def test_shading_uses_per_room_target_position():
    south = CoverInfo(entity_id="cover.s", orientation="south", target_position=30)
    out = shading_policy(_state([], covers=[south], **_SHADE))
    assert out == {cover_lever("cover.s"): 30}  # per-room override beats default


def test_shading_skips_blocked_room():
    south = CoverInfo(
        entity_id="cover.s", orientation="south", target_position=30, blocked=True
    )
    assert shading_policy(_state([], covers=[south], **_SHADE)) == {}


def test_shading_skips_when_no_position_resolved():
    # no per-room target and no house default -> nothing to command.
    out = shading_policy(
        _state([], covers=[_SOUTH], **{**_SHADE, "shading_default": None})
    )
    assert out == {}


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


# --- proportional shading (#6 enhancement) -----------------------------------

def test_proportional_shade_position_scales_with_solar():
    # threshold=200, full=700, default(full)=50: open at threshold, deepest at full,
    # linear between (midpoint 450 -> 75).
    at = lambda s: proportional_shade_position(  # noqa: E731
        s, None, solar_threshold=200.0, full_position=50
    )
    assert at(200) == 100    # just triggering -> barely shaded (open)
    assert at(450) == 75     # halfway -> half of the 100->50 travel
    assert at(700) == 50     # full sun -> the configured deepest shade
    assert at(2000) == 50    # clamped at the deepest (frac capped at 1)


def test_proportional_shade_position_hot_outdoor_deepens():
    cool = proportional_shade_position(450, 20.0, solar_threshold=200.0, full_position=50)
    hot = proportional_shade_position(450, 38.0, solar_threshold=200.0, full_position=50)
    assert hot < cool  # a hot day drives the blind deeper (lower position)
    assert 0 <= hot <= 100


def test_shading_proportional_uses_scaled_position():
    # proportional ON + no per-room target -> scaled position (500 W/m² -> 70), not
    # the flat default (50).
    out = shading_policy(
        _state([], covers=[_SOUTH], **{**_SHADE, "shading_proportional": True})
    )
    assert out == {cover_lever("cover.s"): 70}  # (500-200)/500=0.6 -> 100-30


def test_shading_proportional_per_room_override_still_wins():
    south = CoverInfo(entity_id="cover.s", orientation="south", target_position=30)
    out = shading_policy(
        _state([], covers=[south], **{**_SHADE, "shading_proportional": True})
    )
    assert out == {cover_lever("cover.s"): 30}  # explicit override beats proportional


# --- pre-cool planner (#9) ---------------------------------------------------

def test_precool_nudges_fancoil_setpoints_colder():
    z = _zone("a", climate="climate.a", emitter="fancoil")
    state = _state(
        [z], season=SEASON_SUMMER, setpoint=24.0, offset=0.0,
        duty=True, precool=True, precool_offset=1.5,
    )
    out = precool_policy(state)
    assert out[temperature_lever("climate.a")] == 22.5  # 24 + 0 - 1.5


def test_precool_noop_when_duty_off_or_not_precool_or_winter():
    z = _zone("a", climate="climate.a")
    base = dict(setpoint=24.0, offset=0.0, precool_offset=1.5)
    assert precool_policy(_state([z], season=SEASON_SUMMER, duty=False,
                                 precool=True, **base)) == {}
    assert precool_policy(_state([z], season=SEASON_SUMMER, duty=True,
                                 precool=False, **base)) == {}
    assert precool_policy(_state([z], season="winter", duty=True,
                                 precool=True, **base)) == {}


def test_precool_skips_radiant_and_disabled():
    zones = [
        _zone("rad", climate="climate.rad", emitter="radiant"),
        _zone("dis", climate="climate.dis", emitter="fancoil", enabled=False),
        _zone("ok", climate="climate.ok", emitter="fancoil"),
    ]
    out = precool_policy(_state(
        zones, season=SEASON_SUMMER, setpoint=24.0, offset=0.0,
        duty=True, precool=True, precool_offset=1.5,
    ))
    assert temperature_lever("climate.ok") in out
    assert temperature_lever("climate.rad") not in out
    assert temperature_lever("climate.dis") not in out


# --- #3 v2 comfort-band control + capacity-matched fan -----------------------

def _fanzone(zid, *, temp, bedroom=False, emitter="fancoil", units=None):
    # a leader fancoil zone with one (fan, manuale) unit by default.
    fu = units if units is not None else ((f"fan.{zid}", f"switch.{zid}_man"),)
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter=emitter,
        temp=temp, bedroom=bedroom, fancoil_units=fu,
    )


# band defaults (B=1.5, A=0.75) come from HouseState's None -> DEFAULT; season summer.
_BAND = dict(fan_pacing=True, season="summer", setpoint=24.0, offset=0.0)


def test_band_disabled_returns_nothing():
    assert FanBandController()(_state([_fanzone("a", temp=26.0)])) == {}


def test_band_run_slams_setpoint_low_and_holds_fan():
    out = FanBandController()(_state([_fanzone("a", temp=26.0)], **_BAND))
    assert out[temperature_lever("climate.a")] == 23.25  # 24 - A(0.75)
    assert out["switch:switch.a_man"] == "on"
    assert "fan:fan.a" in out and out["fan:fan.a"] > 0  # capacity-matched run fan


def test_band_rest_slams_setpoint_high_and_fan_to_min():
    # cold room -> REST: setpoint up, fan to fan_min (0 = off, held in manual).
    out = FanBandController()(_state([_fanzone("a", temp=23.0)], **_BAND))
    assert out[temperature_lever("climate.a")] == 24.75  # 24 + A(0.75)
    assert out["switch:switch.a_man"] == "on" and out["fan:fan.a"] == 0


def test_band_drives_all_units_of_one_open_space():
    # living-room-style leader owning Salotto + Cucina -> both fans same speed.
    z = _fanzone(
        "lr", temp=26.0,
        units=(("fan.salotto", "switch.salotto_man"), ("fan.cucina", "switch.cucina_man")),
    )
    out = FanBandController()(_state([z], **_BAND))
    assert out["switch:switch.salotto_man"] == "on"
    assert out["switch:switch.cucina_man"] == "on"
    assert out["fan:fan.salotto"] == out["fan:fan.cucina"]  # one unit, one speed


def test_band_skips_bedroom_during_night():
    out = FanBandController()(
        _state([_fanzone("bed", temp=26.0, bedroom=True)], night=True, **_BAND)
    )
    assert out == {}  # camere silenziose (#2b) owns it -> no emit, no fight


def test_band_releases_manuale_when_disabled():
    c = FanBandController()
    c(_state([_fanzone("a", temp=26.0)], **_BAND))      # managed (manuale on)
    out = c(_state([_fanzone("a", temp=26.0)]))          # fan_pacing now off
    assert out["switch:switch.a_man"] == "off"           # handed back to AUTO


def test_band_skips_followers():
    follower = ZoneSnapshot(
        zone_id="kitchen", name="kitchen", climate=None, emitter="fancoil",
        temp=26.0, follows="lr", fancoil_units=(("fan.cucina", "switch.cucina_man"),),
    )
    assert FanBandController()(_state([follower], **_BAND)) == {}


# --- PV/energy-aware pre-cool wiring into the band controller (F4c-lite) ------

from dataclasses import replace as _replace  # noqa: E402

from custom_components.villa_hvac.const import SCHEDULE_MAX_AGE  # noqa: E402
from custom_components.villa_hvac.supervisor import annotate_centers  # noqa: E402


def _annotated(state):
    """R1: in production the engine resolves the band center onto the snapshots
    (annotate_centers) before the controllers run; direct FanBandController tests
    exercising a center FEATURE (PV/relax/planner) must do the same."""
    return annotate_centers(state, max_age=SCHEDULE_MAX_AGE)


def test_band_pv_bank_drives_center_to_floor():
    # BANK: center pulled to the floor (22) -> RUN slam = 22 - A(0.75).
    s = _replace(
        _state([_fanzone("a", temp=26.0)], **_BAND), pv_mode="bank", pv_floor=22.0
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 21.25


def test_band_pv_bank_never_raises_center():
    # floor above the base center must NOT reduce cooling (min(base, floor)).
    s = _replace(
        _state([_fanzone("a", temp=26.0)], setpoint=21.0, offset=0.0, **{
            k: v for k, v in _BAND.items() if k not in ("setpoint", "offset")
        }),
        pv_mode="bank", pv_floor=22.0,
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 20.25  # 21 - A, floor ignored (higher)


def test_band_pv_coast_defers_within_comfort():
    # COAST raises the center -> a cold room's REST setpoint drifts up (deferred).
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=1.5, duty_comfort_max=27.0,
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 26.25  # (24+1.5) + A(0.75)


def test_band_pv_coast_capped_at_comfort_max():
    # a large relax is clamped at duty_comfort_max (never defers past comfort).
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=5.0, duty_comfort_max=25.0,
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 25.75  # min(24+5, 25) + A


def test_band_pv_hold_is_normal_band():
    s = _replace(_state([_fanzone("a", temp=26.0)], **_BAND), pv_mode="hold")
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 23.25  # unchanged from no-PV RUN


def test_band_pv_coast_respects_comfort_window():
    # comfort enabled + in-window (z.comfort_relax == 0) -> COAST must NOT defer.
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=1.5, duty_comfort_max=27.0,
        comfort_enabled=True,
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 24.75  # tight REST, not deferred


def test_band_pv_coast_defers_outside_comfort_window():
    # comfort enabled but OUT of window (z.comfort_relax > 0) -> defer by max(pv, f4b).
    z = ZoneSnapshot(
        zone_id="a", name="a", climate="climate.a", emitter="fancoil",
        temp=23.0, comfort_relax=1.0,
        fancoil_units=(("fan.a", "switch.a_man"),),
    )
    s = _replace(
        _state([z], **_BAND),
        pv_mode="coast", pv_coast_relax=1.5, duty_comfort_max=27.0,
        comfort_enabled=True,
    )
    out = FanBandController()(_annotated(s))
    assert out[temperature_lever("climate.a")] == 26.25  # (24 + max(1.5,1.0)) + A


# --- F2: ThermalEstimator (online observer) ----------------------------------

from datetime import timedelta  # noqa: E402

from custom_components.villa_hvac.policies import ThermalEstimator  # noqa: E402


def _leader(zid="lr", **kw):
    base = dict(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter="fancoil",
        fancoil_units=((f"fan.{zid}", f"switch.{zid}_man"),),
    )
    base.update(kw)
    return ZoneSnapshot(**base)


def _obs_state(
    z, *, now, outdoor=30.0, solar=0.0, consenso="off", blocco="off", enabled=True
):
    # blocco defaults to "off" (released) — its realistic value while cooling; a
    # transient/None blocco read now bars admitting a k-learning window (B4:
    # observer-blocco-read-poisons-k).
    return HouseState(
        now=now, zones={z.zone_id: z}, outdoor_temp=outdoor, solar=solar,
        consenso_freddo=consenso, blocco=blocco, model_learning_enabled=enabled,
    )


def test_estimator_learns_passive_over_a_window():
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 2, 0, 0)  # night, no cooling -> w=False
    for m in range(0, 17, 2):  # 16 min of samples, temp drifting
        z = _leader(temp=24.0 + 0.4 * (m / 60.0), demand=False)
        est.observe(_obs_state(z, now=base + timedelta(minutes=m)))
    assert est.params["lr"].n >= 1  # a passive update fired after >=15 min


def test_estimator_is_disabled_by_flag():
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 2, 0, 0)
    for m in range(0, 17, 2):
        z = _leader(temp=24.0, demand=False)
        est.observe(_obs_state(z, now=base + timedelta(minutes=m), enabled=False))
    assert "lr" not in est.params  # learning off -> nothing observed


def test_estimator_skips_capacity_window_in_f2a():
    # w=True (valve open + consenso on) -> F2a does NOT learn (k is F2b).
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 14, 0, 0)
    for m in range(0, 17, 2):
        z = _leader(temp=25.0, demand=True)
        est.observe(_obs_state(z, now=base + timedelta(minutes=m), consenso="on"))
    p = est.params.get("lr")
    assert p is None or (p.n == 0 and p.n_k == 0)  # no passive, no capacity yet


def test_estimator_load_rejects_corrupt_and_keeps_valid():
    est = ThermalEstimator()
    est.load({
        "bad": {"a": -1.0, "b": 0.0, "c": 0.0, "k": 1.0, "p": [0.0] * 9, "p_k": 0.0},
        "neg_k": {"a": 0.0, "b": 0.0, "c": 0.0, "k": -0.5, "p": [0.0] * 9, "p_k": 0.0},
        "ok": {"a": 0.1, "b": 0.001, "c": 0.0, "k": 0.9, "p": [0.0] * 9, "p_k": 0.0, "n": 5},
    })
    assert "bad" not in est.params and "neg_k" not in est.params
    assert est.params["ok"].a == 0.1 and est.params["ok"].n == 5


def test_estimator_dump_roundtrips():
    est = ThermalEstimator()
    est.load({"ok": {"a": 0.1, "b": 0.001, "c": 0.2, "k": 0.9, "p": [0.0] * 9,
                     "p_k": 1.0, "n": 5, "n_k": 3}})
    est2 = ThermalEstimator()
    est2.load(est.dump())
    assert est2.params["ok"].a == 0.1 and est2.params["ok"].n_k == 3


def test_estimator_model_for_blends_prior_when_unconverged():
    est = ThermalEstimator()
    # no learning yet -> model_for returns the prior (control behaves like F1).
    m = est.model_for("lr")
    assert m.a > 0 and m.k > 0  # the COOL_* prior


def test_estimator_learns_capacity_on_held_steady_window():
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 14, 0, 0)
    for m in range(0, 17, 2):  # w=True, fan HELD at 50% steady, manuale on
        z = _leader(temp=25.0 - 0.2 * (m / 60.0), demand=True, fan_pct=50, manuale_on=True)
        est.observe(_obs_state(z, now=base + timedelta(minutes=m), consenso="on"))
    assert est.params["lr"].n_k >= 1     # capacity learned
    assert est.params["lr"].n == 0        # passive untouched on a cooling window


def test_estimator_skips_learning_on_transient_consenso():
    """B4: a transient consenso read can't classify the window — skip it (don't
    mislabel a possible cooling window as a passive {a,b,c} window)."""
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 2, 0, 0)
    for m in range(0, 17, 2):
        z = _leader(temp=24.0 + 0.4 * (m / 60.0), demand=False)
        est.observe(
            _obs_state(z, now=base + timedelta(minutes=m), consenso="unavailable")
        )
    assert est.params["lr"].n == 0  # seeded prior, but nothing learned


def test_estimator_skips_k_window_on_transient_blocco():
    """B4 (observer-blocco-read-poisons-k): a k-learning window needs a TRUSTED
    blocco read; a transient blocco could hide an active block -> skip."""
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 14, 0, 0)
    for m in range(0, 17, 2):  # w=True, held-steady fan, but blocco transient
        z = _leader(temp=25.0 - 0.2 * (m / 60.0), demand=True, fan_pct=50, manuale_on=True)
        est.observe(
            _obs_state(
                z, now=base + timedelta(minutes=m), consenso="on", blocco="unavailable"
            )
        )
    p = est.params.get("lr")
    assert p is None or p.n_k == 0


def test_estimator_skips_capacity_when_fan_not_held():
    est = ThermalEstimator()
    base = datetime(2026, 6, 30, 14, 0, 0)
    for m in range(0, 17, 2):  # AUTO (manuale off) -> unknown fan -> never learn k
        z = _leader(temp=25.0, demand=True, fan_pct=100, manuale_on=False)
        est.observe(_obs_state(z, now=base + timedelta(minutes=m), consenso="on"))
    p = est.params.get("lr")
    assert p is None or p.n_k == 0


def test_band_fan_uses_learned_capacity():
    from dataclasses import replace as _replace
    z = _fanzone("a", temp=26.0)
    low_k = _replace(z, model_a=0.0, model_b=0.0, model_c=0.0, model_k=0.5)
    out_low = FanBandController()(_state([low_k], **_BAND))
    out_prior = FanBandController()(_state([z], **_BAND))  # model_* None -> prior k
    # a learned LOW capacity demands at least as much fan as the (higher) prior.
    assert out_low["fan:fan.a"] >= out_prior["fan:fan.a"]


def test_band_comfort_relax_raises_center_to_rest():
    from dataclasses import replace as _replace
    z = _fanzone("a", temp=25.0)
    relaxed = _replace(z, comfort_relax=2.0)
    # relax raises center 24 -> 26; temp 25 < 26-0.75 -> REST (setpoint 26+0.75).
    out = FanBandController()(_annotated(_state([relaxed], **_BAND)))
    assert out[temperature_lever("climate.a")] == 26.75
    # no relax -> center 24, temp 25 >= 24.75 -> RUN (setpoint 24-0.75).
    out0 = FanBandController()(_state([z], **_BAND))
    assert out0[temperature_lever("climate.a")] == 23.25


# --- F3c: RegimeCoordinator + phase override ---------------------------------

from datetime import timedelta as _td  # noqa: E402

from custom_components.villa_hvac.policies import RegimeCoordinator  # noqa: E402


def test_regime_coordinator_yields_when_not_medium():
    rc = RegimeCoordinator()
    ov, bl = rc.step(
        _state([_fanzone("a", temp=25.0)], **_BAND),
        regime="low", center=24.0, min_on=_td(minutes=10), min_off=_td(minutes=10),
    )
    assert ov == {} and bl is None  # yields -> duty BLOCCO survives


def test_regime_coordinator_coalesces_in_medium():
    rc = RegimeCoordinator()
    ov, bl = rc.step(
        _state([_fanzone("a", temp=26.0)], **_BAND),
        regime="medium", center=24.0, min_on=_td(minutes=10), min_off=_td(minutes=10),
    )
    assert bl == "off"                  # BLOCCO_RELEASE (coalescing rests via setpoint)
    assert ov.get("a") == "run"          # hot room -> house RUN


def test_band_phase_override_forces_phase():
    z = _fanzone("a", temp=26.0)         # would RUN on its own
    out = FanBandController()(_state([z], **_BAND), phase_override={"a": "rest"})
    assert out[temperature_lever("climate.a")] == 24.75   # forced REST -> center+slam
    assert out["fan:fan.a"] == 0                          # rest -> fan_min


def test_duty_controller_emits_release_when_disabled():
    """Disabling duty must emit an explicit BLOCCO_RELEASE, not a silent {} — an
    empty dict drops the lever from `desired`, so a block asserted just before
    disable would never be actively cleared."""
    from custom_components.villa_hvac.policies import DutyController
    from custom_components.villa_hvac.supervisor import BLOCCO_LEVER, BLOCCO_RELEASE

    out = DutyController()(_state([_zone("living_room")], duty=False))
    assert out == {BLOCCO_LEVER: BLOCCO_RELEASE}


def test_duty_freezes_stint_on_transient_consenso():
    """B4: a transient consenso read must NOT reset the stint timer — otherwise a
    dropped KNX telegram lets the villa re-accrue a fresh full stint each time."""
    from dataclasses import replace

    from custom_components.villa_hvac.policies import DutyController
    from custom_components.villa_hvac.supervisor import (
        BLOCCO_BLOCK,
        BLOCCO_LEVER,
        BLOCCO_RELEASE,
        DutyState,
    )

    base = replace(
        _state([_zone("living_room")], duty=True, consenso="unavailable"),
        duty_max_stint=timedelta(minutes=120), duty_cooloff=timedelta(minutes=30),
    )

    c = DutyController()
    c._duty = DutyState(stint_start=T0)
    out = c(base)
    assert out == {BLOCCO_LEVER: BLOCCO_RELEASE}   # no cooloff -> release
    assert c._duty.stint_start == T0                # timer FROZEN (not reset)

    c2 = DutyController()
    c2._duty = DutyState(cooloff_until=T0 + timedelta(minutes=10))
    out2 = c2(replace(base, consenso_freddo="unknown"))
    assert out2 == {BLOCCO_LEVER: BLOCCO_BLOCK}     # mid-cooloff -> keep blocking
    assert c2._duty.cooloff_until == T0 + timedelta(minutes=10)  # held


def test_comfort_breach_ignores_non_cooling_zones():
    """The duty comfort-breach counts only actively-managed cooling leaders. A hot
    radiant/split room (fused temp but no fancoil) must NOT trip it — otherwise a
    warm bathroom aborts every duty cooloff forever (ENGINE_REVIEW §4)."""
    from dataclasses import replace

    from custom_components.villa_hvac.policies import _comfort_breach

    leader = _fanzone("living_room", temp=24.0)          # comfortable cooling leader
    hot_bath = ZoneSnapshot(                              # radiant, no fancoil units
        zone_id="bagno", name="bagno", climate="climate.bagno",
        emitter="radiant", temp=30.0,
    )
    state = replace(
        _state([leader, hot_bath], season=SEASON_SUMMER), duty_comfort_max=27.0
    )
    assert _comfort_breach(state) is False               # hot bath ignored

    hot_leader = replace(
        _state([_fanzone("living_room", temp=28.0)], season=SEASON_SUMMER),
        duty_comfort_max=27.0,
    )
    assert _comfort_breach(hot_leader) is True            # a hot leader trips it


# --- D1: identifiability gating (F4c Phase 4) --------------------------------

def test_abc_identified_requires_solar_excitation():
    from custom_components.villa_hvac.supervisor import ThermalParams, abc_identified

    # high passive count but sunless windows (s_hi ~ 0) -> b never excited -> NO.
    night = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=100, s_hi=0.0)
    assert abc_identified(night, conf_min=40, solar_excitation_min=150) is False
    # high count + a real daytime passive window seen -> identified.
    day = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=100, s_hi=400.0)
    assert abc_identified(day, conf_min=40, solar_excitation_min=150) is True
    # excited but too few updates -> not yet identified.
    fresh = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=5, s_hi=400.0)
    assert abc_identified(fresh, conf_min=40, solar_excitation_min=150) is False


def test_planner_eligible_needs_abc_and_converged_k():
    from custom_components.villa_hvac.supervisor import ThermalParams, planner_eligible

    kw = dict(abc_conf_min=40, k_conf_min=20, solar_excitation_min=150)
    # abc identified but k not converged (n_k=0) -> advisory, not eligible.
    no_k = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=100, n_k=0, s_hi=400.0)
    assert planner_eligible(no_k, **kw) is False
    # abc identified + k converged -> eligible.
    ok = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=100, n_k=100, s_hi=400.0)
    assert planner_eligible(ok, **kw) is True
    # k converged but abc not solar-excited -> not eligible.
    no_sun = ThermalParams(a=0.03, b=0.0008, c=0.0, k=1.2, n=100, n_k=100, s_hi=0.0)
    assert planner_eligible(no_sun, **kw) is False


def test_passive_update_tracks_solar_excitation():
    from custom_components.villa_hvac.supervisor import (
        ParamBounds,
        rls_passive_update,
        seed_params,
    )

    p = seed_params(0.03, 0.0008, 0.0, 1.2, p0_passive=(0.5, 1e-5, 4.0), p0_k=4.0)
    bounds = ParamBounds(0.5, 0.01, 3.0, 0.1, 5.0)
    kw = dict(dt_dt=0.1, t_out=25.0, temp=24.0, forgetting=0.995, bounds=bounds)
    p2 = rls_passive_update(p, solar=400.0, **kw)
    assert p2.s_hi == 400.0                       # a sunny passive window raises it
    p3 = rls_passive_update(p2, solar=0.0, **kw)   # a later night window
    assert p3.s_hi == 400.0                        # never lowers the max


def test_estimator_exposes_planner_eligibility():
    est = ThermalEstimator()
    ok = {"a": 0.03, "b": 0.0008, "c": 0.0, "k": 1.2, "p": [0.0] * 9,
          "p_k": 0.0, "n": 100, "n_k": 100, "s_hi": 400.0}
    night = {**ok, "s_hi": 0.0}
    est.load({"day": ok, "night": night})
    assert est.solar_excitation("day") == 400.0
    assert est.abc_identified("day") is True and est.planner_eligible("day") is True
    assert est.abc_identified("night") is False and est.planner_eligible("night") is False
    assert est.planner_eligible("unknown") is False  # no model -> not eligible


def test_estimator_persists_solar_excitation():
    est = ThermalEstimator()
    est.load({"lr": {"a": 0.03, "b": 0.0008, "c": 0.0, "k": 1.2, "p": [0.0] * 9,
                     "p_k": 0.0, "n": 5, "s_hi": 320.0}})
    dumped = est.dump()
    assert dumped["lr"]["s_hi"] == 320.0
    est2 = ThermalEstimator()
    est2.load(dumped)
    assert est2.solar_excitation("lr") == 320.0


# --- Phase 6: FanBandController driven by the unified planner reference -------

def _sched_for(zid, center, *, created):
    from custom_components.villa_hvac.supervisor import (
        CenterPoint,
        CenterSchedule,
        ZoneCenterSchedule,
    )
    return CenterSchedule(
        zones={zid: ZoneCenterSchedule(
            zone_id=zid, points=(CenterPoint(minute=0, center=center),), eligible=True,
        )},
        created_at=created,
    )


def test_band_planner_drives_eligible_zone():
    """Switch on + schedule fresh + zone eligible -> the band center is the planner
    reference (23.0), not the ladder base (24.0). RUN slam = 23 - 0.75 = 22.25."""
    from dataclasses import replace as _replace

    z = _replace(_fanzone("a", temp=26.0), model_planner_eligible=True)
    sched = _sched_for("a", 23.0, created=T0)
    st = _replace(
        _state([z], **_BAND), center_schedule=sched, unified_planner_enabled=True,
        comfort_floor=22.0, duty_comfort_max=27.0,
    )
    out = FanBandController()(_annotated(st))
    assert out[temperature_lever("climate.a")] == 22.25   # 23 (ref) - 0.75


def test_band_uses_ladder_when_planner_switch_off():
    """Switch off -> the ladder drives (base 24). RUN slam = 24 - 0.75 = 23.25."""
    from dataclasses import replace as _replace

    z = _replace(_fanzone("a", temp=26.0), model_planner_eligible=True)
    sched = _sched_for("a", 23.0, created=T0)
    st = _replace(
        _state([z], **_BAND), center_schedule=sched, unified_planner_enabled=False,
        comfort_floor=22.0, duty_comfort_max=27.0,
    )
    out = FanBandController()(_annotated(st))
    assert out[temperature_lever("climate.a")] == 23.25   # ladder base 24 - 0.75


def test_band_uses_ladder_when_zone_not_planner_eligible():
    """Switch on but the zone is NOT planner-eligible (hard room) -> ladder."""
    from dataclasses import replace as _replace

    z = _fanzone("a", temp=26.0)  # model_planner_eligible defaults False
    sched = _sched_for("a", 23.0, created=T0)
    st = _replace(
        _state([z], **_BAND), center_schedule=sched, unified_planner_enabled=True,
        comfort_floor=22.0, duty_comfort_max=27.0,
    )
    out = FanBandController()(_annotated(st))
    assert out[temperature_lever("climate.a")] == 23.25   # advisory -> ladder
