"""v0.81–v0.82 — winter brain W2 (setback advisor), W3 (winter return
pre-conditioning), W4 (PV heating)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac import returnhome as rh
from custom_components.villa_hvac.const import SEASON_SUMMER, SEASON_WINTER
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    preset_lever,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor.winter_model import WinterParams
from custom_components.villa_hvac.supervisor.winter_plan import (
    DWELL_ON,
    MIN_OFF,
    MIN_ON,
    PvHeatState,
    WinterRoom,
    outdoor_estimate,
    pv_surplus_step,
    winter_setback_advice,
)
from custom_components.villa_hvac.winter import PvHeatController

NOW = datetime(2027, 1, 11, 8, 0, tzinfo=timezone.utc)   # Monday 08:00
P = WinterParams(a=0.03, k_h=0.6, lag_min=45)
SLOW = WinterParams(a=0.03, k_h=0.25, lag_min=90)


def _room(zone="r", temp=21.0, target=21.0, params=P):
    return WinterRoom(zone=zone, name=zone, temp=temp, target=target, params=params)


def _advice(rooms, hours, outdoor=5.0, floor=16.0, max_depth=6.0):
    return winter_setback_advice(
        rooms, now=NOW, eta=NOW + timedelta(hours=hours), outdoor=outdoor,
        floor=floor, max_depth=max_depth,
    )


# --- W2 -------------------------------------------------------------------------

def test_weekend_absence_reaches_a_deep_setback_bounded_by_floor_and_max():
    a = _advice([_room()], hours=4 * 24 + 10)          # back Friday 18:00
    assert a.depth == 5.0                               # floor 16 = 21 − 5
    assert NOW < a.start < a.eta
    assert a.rooms[0].setback == 16.0 and a.rooms[0].temp_at_start == 16.0
    assert a.drop == 5.0 and a.saved_dh > 0
    assert _advice([_room()], hours=100, max_depth=3.0).depth == 3.0


def test_short_absence_barely_drops_and_still_recovers_in_time():
    short = _advice([_room()], hours=3)
    long = _advice([_room()], hours=48)
    assert short.drop < long.drop and short.drop <= 0.6
    p = short.rooms[0]
    from custom_components.villa_hvac.supervisor.winter_model import recovery_minutes
    rec = recovery_minutes(p.temp_at_start, p.target, 5.0, P)
    assert p.start + timedelta(minutes=rec) <= short.eta + timedelta(minutes=1)


def test_the_slowest_room_starts_first_and_drives_the_house_start():
    a = _advice([_room("fast"), _room("slow", params=SLOW)], hours=24)
    assert a.limiting_room == "slow"
    fast_plan = next(p for p in a.rooms if p.zone == "fast")
    slow_plan = next(p for p in a.rooms if p.zone == "slow")
    assert fast_plan.setback == slow_plan.setback       # one uniform depth
    assert slow_plan.start < fast_plan.start == fast_plan.start
    assert a.start == slow_plan.start


def test_cold_room_already_too_far_preheats_now():
    a = _advice([_room(temp=17.0)], hours=2)
    assert a.start == NOW


def test_no_advice_without_eta_or_in_the_past():
    assert winter_setback_advice([_room()], now=NOW, eta=None, outdoor=5,
                                 floor=16, max_depth=6) is None
    assert _advice([_room()], hours=-1) is None


def test_outdoor_estimate_is_conservative():
    fc = [(NOW + timedelta(hours=h), t) for h, t in ((1, 2.0), (2, 4.0), (30, -5.0))]
    assert outdoor_estimate(fc, NOW, NOW + timedelta(hours=3), 6.0) == 3.0
    assert outdoor_estimate([], NOW, NOW + timedelta(hours=3), 6.0) == 6.0


# --- W3 -------------------------------------------------------------------------

def _patch_return(monkeypatch, opt_in=True, armed=True):
    monkeypatch.setattr(rh, "return_precond_enabled", lambda h, e: opt_in)
    monkeypatch.setattr(rh, "return_armed", lambda h, e: armed)


def _via(now):
    z = ZoneSnapshot(zone_id="r", name="r", climate="climate.r", emitter="radiant",
                     temp=19.0, enabled=True)
    return HouseState(now=now, zones={"r": z}, season=SEASON_WINTER,
                      house_mode="Via", house_setpoint=21.0, mode_offset=-2.0)


def test_winter_return_holds_the_advised_depth_then_preheats(monkeypatch):
    _patch_return(monkeypatch)
    advice = _advice([_room()], hours=48)
    ctrl = rh.AwayReturnController()
    waiting = ctrl.apply(_via(NOW), None, None, commit=True, winter_advice=advice)
    assert waiting.house_mode == "Via" and waiting.mode_offset == -advice.depth
    assert ctrl.decision == rh.RETURN_WAITING
    pre = ctrl.apply(_via(advice.start + timedelta(minutes=1)), None, None,
                     commit=True, winter_advice=advice)
    assert pre.house_mode == "Casa" and pre.mode_offset == 0.0


def test_winter_return_inert_without_advice_or_unarmed(monkeypatch):
    _patch_return(monkeypatch, armed=False)
    src = _via(NOW)
    ctrl = rh.AwayReturnController()
    assert ctrl.apply(src, None, None, commit=True,
                      winter_advice=_advice([_room()], hours=48)) is src
    _patch_return(monkeypatch)
    assert ctrl.apply(src, None, None, commit=True, winter_advice=None) is src


# --- W4 -------------------------------------------------------------------------

def _pv(state, t, soc=95.0, grid=-800.0, batt=-200.0, sun=True):
    return pv_surplus_step(state, now=t, soc=soc, grid_w=grid, battery_w=batt, sun_up=sun)


def test_pv_latch_needs_soc_outflow_and_dwell():
    s = _pv(PvHeatState(), NOW)
    assert not s.active
    s = _pv(s, NOW + DWELL_ON)
    assert s.active
    assert not _pv(PvHeatState(), NOW + DWELL_ON, soc=85.0).active      # SoC < 90
    no_flow = _pv(_pv(PvHeatState(), NOW, grid=50, batt=100), NOW + DWELL_ON,
                  grid=50, batt=100)
    assert not no_flow.active        # another unit uses the PV: no net outflow


def test_pv_latch_judges_net_flow_not_just_outflow():
    """Review MAJOR: grid import + battery charging is NOT a surplus."""
    s = _pv(PvHeatState(), NOW, grid=2000, batt=-400)
    s = _pv(s, NOW + DWELL_ON, grid=2000, batt=-400)
    assert not s.active


def test_pv_latch_stops_on_sustained_deficit_sunset_or_lost_data():
    on = PvHeatState(active=True, since=NOW)
    # a modest draw on the battery is tolerated
    assert _pv(on, NOW + timedelta(minutes=5), soc=88, grid=0, batt=800).active
    # sustained deficit (our heat pump draining the battery) after MIN_ON -> off
    t = NOW + MIN_ON + timedelta(minutes=1)
    s = _pv(on, t, grid=200, batt=1500)
    assert s.active
    s = _pv(s, t + timedelta(minutes=10), grid=200, batt=1500)
    assert not s.active and s.reason == "ended (deficit)"
    # sunset: immediate
    assert not _pv(on, NOW + timedelta(minutes=2), sun=False).active
    # lost Condominio data must not keep the rooms lifted (review MAJOR)
    s = pv_surplus_step(on, now=t, soc=None, grid_w=None, battery_w=None, sun_up=True)
    assert s.active
    s = pv_surplus_step(s, now=t + timedelta(minutes=10), soc=None, grid_w=None,
                        battery_w=None, sun_up=True)
    assert not s.active
    assert not pv_surplus_step(on, now=NOW + timedelta(hours=10), soc=None,
                               grid_w=None, battery_w=None, sun_up=False).active
    # rest after stopping
    off = PvHeatState(active=False, since=NOW)
    assert _pv(off, NOW + MIN_OFF - timedelta(minutes=1)).reason == "rest"


def test_pv_heat_needs_auto_setback():
    """Review MAJOR: with Auto setback off nothing would restore the setback."""
    from dataclasses import replace as _r
    assert _active_ctrl()(_r(_pv_house("Via", -2.0), auto_setback=False)) == {}


def _pv_house(mode, offset, *, paused=False, enabled=True, season=SEASON_WINTER):
    zones = {
        zid: ZoneSnapshot(zone_id=zid, name=zid, climate=f"climate.{zid}",
                          emitter="radiant", enabled=enabled, paused=paused,
                          setpoint_offset=0.5 if zid == "main_bedroom" else 0.0)
        for zid in ("main_bedroom", "office")
    }
    return HouseState(
        now=NOW, zones=zones, season=season, house_mode=mode, house_setpoint=21.0,
        mode_offset=offset, pv_heat_enabled=True, auto_setback=True,
        pv_heat_zones=frozenset({"main_bedroom"}), sun_elevation=20.0,
        condo_soc=96.0, condo_grid_w=-1200.0, condo_battery_w=0.0,
    )


def _active_ctrl():
    c = PvHeatController()
    c.state = PvHeatState(active=True, since=NOW - timedelta(hours=1))
    return c


def test_pv_heat_takes_chosen_rooms_to_casa_even_in_vacation():
    out = _active_ctrl()(_pv_house("Vacanza", None))
    assert out == {
        preset_lever("climate.main_bedroom"): "comfort",
        temperature_lever("climate.main_bedroom"): 21.5,     # Casa incl. room trim
    }
    out = _active_ctrl()(_pv_house("Via", -2.0))
    assert out[temperature_lever("climate.main_bedroom")] == 21.5


def test_pv_heat_is_silent_in_casa_paused_disabled_or_summer():
    assert _active_ctrl()(_pv_house("Casa", 0.0)) == {}
    assert _active_ctrl()(_pv_house("Via", -2.0, paused=True)) == {}
    assert _active_ctrl()(_pv_house("Via", -2.0, enabled=False)) == {}
    assert _active_ctrl()(_pv_house("Via", -2.0, season=SEASON_SUMMER)) == {}


async def test_engine_advice_from_the_return_entities(hass):
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.villa_hvac.const import DOMAIN, SEASON_REFERENCE_CLIMATE
    from custom_components.villa_hvac.engine import build_house_state

    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "5")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)

    def eid(domain, suffix):
        return reg.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{suffix}")

    for suffix in ("winter_setback_advice", "pv_heat_status", "pv_heat",
                   "main_bedroom_pv_heat", "office_pv_heat"):
        domain = "switch" if suffix.endswith("pv_heat") else "sensor"
        assert eid(domain, suffix), suffix
    back = (dt_util.now() + timedelta(days=3)).date()
    hass.states.async_set(eid("date", "return_date"), back.isoformat())
    hass.states.async_set(eid("select", "return_daypart"), "sera")
    engine = entry.runtime_data.engine
    entry.runtime_data.data = {
        **(entry.runtime_data.data or {}),
        "zone_temps": {"main_bedroom": {"value": 20.5}},
    }
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.pv_heat_zones >= {"main_bedroom", "living_room"}
    assert "office" not in state.pv_heat_zones
    advice = engine._winter_advice(state)
    assert advice is not None and advice.depth == 5.0       # 21 − floor 16
    assert [p.zone for p in advice.rooms] == ["main_bedroom"]
    assert advice.start < advice.eta


def test_economy_room_under_the_floor_does_not_cancel_the_house_setback():
    """Review MAJOR: an Economy/trimmed room at or below the floor used to cap
    the uniform depth at 0 — arming the return held the whole house at Casa."""
    a = _advice([_room("eco", target=15.0), _room("bed")], hours=72)
    assert a.depth == 5.0
    a = _advice([_room("eco", target=18.0), _room("bed")], hours=72)
    assert a.depth == 2.0                     # limited by eco 18 − 16


def test_never_shallower_than_native_via():
    a = winter_setback_advice([_room()], now=NOW, eta=NOW + timedelta(hours=72),
                              outdoor=5.0, floor=20.5, max_depth=6.0, min_depth=2.0)
    assert a.depth == 2.0


def test_winter_precond_holds_past_the_eta_until_presence(monkeypatch):
    """Review MAJOR: at the ETA the advice disappears; the house must stay in
    Casa (hold & wait for presence), not drop back to Via."""
    _patch_return(monkeypatch)
    advice = _advice([_room()], hours=48)
    ctrl = rh.AwayReturnController()
    ctrl.apply(_via(advice.start + timedelta(minutes=1)), None, None, commit=True,
               winter_advice=advice)
    late = ctrl.apply(_via(advice.eta + timedelta(hours=2)), None, None,
                      commit=True, winter_advice=None)
    assert late.house_mode == "Casa" and late.mode_offset == 0.0
    _patch_return(monkeypatch, armed=False)                 # disarm -> released
    assert ctrl.apply(_via(advice.eta + timedelta(hours=3)), None, None,
                      commit=True, winter_advice=None).house_mode == "Via"
