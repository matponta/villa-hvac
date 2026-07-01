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


def test_band_pv_bank_drives_center_to_floor():
    # BANK: center pulled to the floor (22) -> RUN slam = 22 - A(0.75).
    s = _replace(
        _state([_fanzone("a", temp=26.0)], **_BAND), pv_mode="bank", pv_floor=22.0
    )
    out = FanBandController()(s)
    assert out[temperature_lever("climate.a")] == 21.25


def test_band_pv_bank_never_raises_center():
    # floor above the base center must NOT reduce cooling (min(base, floor)).
    s = _replace(
        _state([_fanzone("a", temp=26.0)], setpoint=21.0, offset=0.0, **{
            k: v for k, v in _BAND.items() if k not in ("setpoint", "offset")
        }),
        pv_mode="bank", pv_floor=22.0,
    )
    out = FanBandController()(s)
    assert out[temperature_lever("climate.a")] == 20.25  # 21 - A, floor ignored (higher)


def test_band_pv_coast_defers_within_comfort():
    # COAST raises the center -> a cold room's REST setpoint drifts up (deferred).
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=1.5, duty_comfort_max=27.0,
    )
    out = FanBandController()(s)
    assert out[temperature_lever("climate.a")] == 26.25  # (24+1.5) + A(0.75)


def test_band_pv_coast_capped_at_comfort_max():
    # a large relax is clamped at duty_comfort_max (never defers past comfort).
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=5.0, duty_comfort_max=25.0,
    )
    out = FanBandController()(s)
    assert out[temperature_lever("climate.a")] == 25.75  # min(24+5, 25) + A


def test_band_pv_hold_is_normal_band():
    s = _replace(_state([_fanzone("a", temp=26.0)], **_BAND), pv_mode="hold")
    out = FanBandController()(s)
    assert out[temperature_lever("climate.a")] == 23.25  # unchanged from no-PV RUN


def test_band_pv_coast_respects_comfort_window():
    # comfort enabled + in-window (z.comfort_relax == 0) -> COAST must NOT defer.
    s = _replace(
        _state([_fanzone("a", temp=23.0)], **_BAND),
        pv_mode="coast", pv_coast_relax=1.5, duty_comfort_max=27.0,
        comfort_enabled=True,
    )
    out = FanBandController()(s)
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
    out = FanBandController()(s)
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


def _obs_state(z, *, now, outdoor=30.0, solar=0.0, consenso="off", enabled=True):
    return HouseState(
        now=now, zones={z.zone_id: z}, outdoor_temp=outdoor, solar=solar,
        consenso_freddo=consenso, model_learning_enabled=enabled,
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
    out = FanBandController()(_state([relaxed], **_BAND))
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
