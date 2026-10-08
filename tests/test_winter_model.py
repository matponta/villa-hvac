"""v0.80.0 — STORY_WINTER_BRAIN W1: radiant observer + time-to-temperature."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import (
    DOMAIN,
    HEAT_VALVES,
    SEASON_REFERENCE_CLIMATE,
    ZONES,
)
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.supervisor.winter_model import (
    PRIOR_A,
    PRIOR_K,
    WinterModel,
    WinterParams,
    coast_temp,
    recovery_minutes,
)

T0 = datetime(2027, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_every_heat_valve_maps_to_a_thermostat_zone():
    assert len(HEAT_VALVES) == 16  # 15 valves, bagno padronale shared by 01/02
    assert all(ZONES[z].get("climate") for z in HEAT_VALVES)
    assert "pianerottolo_p2" not in HEAT_VALVES


def test_recovery_closed_form():
    p = WinterParams(a=0.05, k_h=1.0, lag_min=30)
    assert recovery_minutes(21.5, 21.0, 5.0, p) == 0.0
    # T_inf = 5 + 1/0.05 = 25 ; t = ln((25-18)/(25-21))/0.05 h
    want = 30 + math.log(7 / 4) / 0.05 * 60
    assert abs(recovery_minutes(18.0, 21.0, 5.0, p) - want) < 1e-6
    # unreachable: T_inf below target
    assert recovery_minutes(18.0, 21.0, -20.0, p) is None
    assert recovery_minutes(None, 21.0, 5.0, p) is None


def test_coast_down_is_floored_at_the_setback():
    p = WinterParams(a=0.05)
    assert abs(coast_temp(21.0, 5.0, 10, p) - (5 + 16 * math.exp(-0.5))) < 1e-9
    assert coast_temp(21.0, 5.0, 100, p, floor=17.0) == 17.0


def _drive(m, zone, *, start, minutes, valve, temp_fn, outdoor=5.0, solar=0.0):
    for i in range(0, minutes + 1, 5):
        t = start + timedelta(minutes=i)
        m.observe(zone, now=t, temp=temp_fn(i), outdoor=outdoor, valve=valve, solar=solar)


def test_learns_loss_on_a_long_closed_night_window():
    m = WinterModel()
    a_true = 0.04
    # closed valve, exponential coast from 21 °C toward 5 °C, 6 h at night
    _drive(m, "z", start=T0, minutes=360, valve=False,
           temp_fn=lambda i: 5 + 16 * math.exp(-a_true * i / 60))
    p = m.get("z")
    assert p.n_a >= 2
    assert abs(p.a - a_true) < 0.008
    assert p.n_k == 0 and p.k_h == PRIOR_K


def test_skips_sunny_windows_and_needs_delta_t():
    m = WinterModel()
    _drive(m, "z", start=T0, minutes=360, valve=False, solar=300.0,
           temp_fn=lambda i: 21 - 0.005 * i)
    assert m.get("z").n_a == 0
    m2 = WinterModel()
    _drive(m2, "z", start=T0, minutes=360, valve=False, outdoor=20.0,
           temp_fn=lambda i: 21 - 0.001 * i)
    assert m2.get("z").n_a == 0  # ΔT < 3 °C: no loss sample


def test_learns_lag_and_heating_rate_on_an_open_window():
    m = WinterModel()
    lag, k_true = 40, 0.8
    a = PRIOR_A

    def temp(i):  # flat until the dead time, then rises at k - a*(T-To)
        return 18.0 if i < lag else 18.0 + (k_true - a * 13) * (i - lag) / 60

    m.observe("z", now=T0 - timedelta(minutes=5), temp=18.0, outdoor=5.0,
              valve=False, solar=0.0)                      # the observed edge
    _drive(m, "z", start=T0, minutes=240, valve=True, temp_fn=temp)
    p = m.get("z")
    assert p.n_lag == 1 and abs(p.lag_min - lag) <= 10   # dead time, climb removed
    assert p.n_k >= 1 and abs(p.k_h - k_true) < 0.15


def test_gap_restarts_the_window_and_dump_load_roundtrip():
    m = WinterModel()
    _drive(m, "z", start=T0, minutes=100, valve=False, temp_fn=lambda i: 21 - 0.01 * i)
    m.observe("z", now=T0 + timedelta(minutes=105), temp=None, outdoor=5, valve=False, solar=0)
    assert "z" not in m._seg
    m.params["z"] = WinterParams(a=0.05, k_h=0.9, lag_min=50, n_a=3, n_k=2, n_lag=1)
    m2 = WinterModel()
    m2.load(m.dump())
    assert m2.get("z") == m.get("z")
    m2.load({"bad": {"a": "x"}, "z2": {"a": 9, "k_h": 0.5, "lag_min": 30}})
    assert "bad" not in m2.params and m2.get("z2").a == 0.2  # clamped


async def test_engine_feeds_valves_and_sensors_in_winter(hass):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    hass.states.async_set(HEAT_VALVES["main_bedroom"], "on")
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "5")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.zones["main_bedroom"].heat_demand is True
    assert state.zones["office"].heat_demand is None   # no state set
    engine._observe_winter(state)
    view = engine.winter_view["main_bedroom"]
    assert view["valve"] is True and view["target"] == 21.0
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_house_heat_up")
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_winter_main_bedroom")
    # summer: no winter view (and nothing learned)
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "cool", {"preset_mode": "comfort"})
    engine._observe_winter(build_house_state(hass, entry, entry.runtime_data))
    assert engine.winter_view == {}


def test_lag_not_learned_from_a_segment_without_an_observed_opening():
    """Review: after a restart / data gap mid-episode the room is already
    climbing — back-extrapolating would clamp the lag to ~0."""
    m = WinterModel()
    _drive(m, "z", start=T0, minutes=120, valve=True,
           temp_fn=lambda i: 19.0 + 0.4 * i / 60)
    assert m.get("z").n_lag == 0


def test_v082_bounds_and_bad_lag_discarded_on_load():
    from custom_components.villa_hvac.supervisor.winter_model import BOUNDS_A, PRIOR_LAG_MIN
    assert BOUNDS_A[0] <= 0.002          # well-insulated rooms are not floored
    m = WinterModel()
    m.load({"bagno_giochi": {"a": 0.005, "k_h": 0.6, "lag_min": 5.0,
                             "n_a": 2, "n_k": 0, "n_lag": 1}})
    p = m.get("bagno_giochi")
    assert p.lag_min == PRIOR_LAG_MIN and p.n_lag == 0 and p.n_a == 2


async def test_no_winter_learning_while_aired_and_for_an_hour_after(hass):
    from dataclasses import replace as _r

    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "5")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    seen = []
    engine.winter.observe = lambda zone, **kw: seen.append((zone, kw["temp"]))
    base = build_house_state(hass, entry, entry.runtime_data)
    z = _r(base.zones["bagno_giochi"], temp=21.0)

    def run(paused, minutes):
        st = _r(base, now=base.now + timedelta(minutes=minutes),
                zones={**base.zones, "bagno_giochi": _r(z, paused=paused)})
        seen.clear()
        engine._observe_winter(st)
        return dict(seen)["bagno_giochi"]

    assert run(False, 0) == 21.0
    assert run(True, 1) is None           # window open
    assert run(False, 30) is None         # settling after the window closed
    assert run(False, 62) == 21.0         # back to learning
