"""v0.71.0 per-unit telemetry: rolling valve duty / strokes + delivered fan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.coordinator import VillaHvacCoordinator
from custom_components.villa_hvac.supervisor.telemetry import (
    HouseTelemetry,
    ValveWindow,
)

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _feed(win: ValveWindow, pattern: list[bool | None], step_s: int = 30) -> datetime:
    t = T0
    for v in pattern:
        win.add(t, v)
        t += timedelta(seconds=step_s)
    return t - timedelta(seconds=step_s)


def test_duty_is_time_weighted_fraction_open():
    win = ValveWindow()
    # 20 min open then 20 min closed, sampled every 30 s
    last = _feed(win, [True] * 40 + [False] * 40)
    st = win.stats(last)
    assert st.strokes == 0            # started open: no CLOSED→OPEN edge seen
    assert abs(st.duty - 0.5) < 0.02


def test_strokes_count_closed_to_open_edges_only():
    win = ValveWindow()
    pattern = ([False] * 4 + [True] * 4) * 5   # 5 openings in 20 min
    last = _feed(win, pattern)
    assert win.stats(last).strokes == 5


def test_unknown_samples_are_neither_open_nor_closed():
    win = ValveWindow()
    last = _feed(win, [True] * 10 + [None] * 10 + [False] * 10)
    st = win.stats(last)
    # only the 20 known-state intervals are credited; unknown→open is no stroke
    assert abs(st.duty - 0.5) < 0.06
    assert st.observed_s < 30 * 30


def test_unknown_between_closed_and_open_still_counts_the_edge():
    win = ValveWindow()
    last = _feed(win, [False] * 5 + [None] * 2 + [True] * 5)
    assert win.stats(last).strokes == 1


def test_gap_longer_than_max_is_not_credited():
    win = ValveWindow()
    win.add(T0, True)
    win.add(T0 + timedelta(minutes=10), False)   # 10 min silence: not credited
    st = win.stats(T0 + timedelta(minutes=10, seconds=30))
    assert st.duty == 0.0
    assert st.observed_s == 30.0


def test_window_rolls_old_samples_out():
    win = ValveWindow()
    _feed(win, [True] * 120)                     # 60 min open
    t = T0 + timedelta(minutes=60)
    for _ in range(120):                          # then 60 min closed
        win.add(t, False)
        t += timedelta(seconds=30)
    st = win.stats(t - timedelta(seconds=30))
    assert st.duty < 0.02


def test_out_of_order_sample_is_ignored():
    win = ValveWindow()
    win.add(T0, True)
    win.add(T0 - timedelta(seconds=5), False)
    assert win.stats(T0 + timedelta(seconds=30)).duty == 1.0


def test_house_telemetry_view():
    tel = HouseTelemetry()
    assert tel.get("office") is None
    tel.observe(T0, {"office": (False, 100, False)})
    tel.observe(T0 + timedelta(seconds=30), {"office": (True, 100, False)})
    u = tel.get("office")
    assert u.valve_open is True and u.fan_delivered == 100 and u.manuale_on is False
    assert u.valve_strokes == 1


async def test_seed_cycles_base_is_monotonic(hass):
    coord = VillaHvacCoordinator(hass, MockConfigEntry(domain=DOMAIN))
    coord.seed_cycles_base(41)
    coord.cool_cycles = 2
    assert coord.cool_starts_total == 43
    coord.seed_cycles_base(-1)
    assert coord.cool_starts_total == 43


async def test_telemetry_sensors_and_valve_based_demand(hass):
    hass.states.async_set("climate.salotto_termostato_2", "cool", {"preset_mode": "comfort"})
    hass.states.async_set("binary_sensor.fancoil_studio_pianerottolo_p1_valvola", "on")
    hass.states.async_set("binary_sensor.fancoil_camera_padronale_valvola", "off")
    hass.states.async_set("fan.fancoil_studio_pianerottolo_p1", "on", {"percentage": 67})
    hass.states.async_set("switch.fancoil_studio_pianerottolo_p1_manuale", "off")
    # padronale fan runs at 100% in AUTO with its valve CLOSED: NOT demand
    hass.states.async_set("fan.fancoil_camera_padronale", "on", {"percentage": 100})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    await engine._tick()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    demand = hass.states.get("sensor.cooling_demand_zones")
    assert demand.state == "1"
    assert demand.attributes["zones"] == ["office"]
    assert "fan.fancoil_camera_padronale" in demand.attributes["fans_running"]

    fan = hass.states.get("sensor.office_studio_fan_delivered")
    assert fan.state == "67"
    assert fan.attributes["mode"] == "auto"
    assert fan.attributes["owner"] == "knx_auto"
    assert hass.states.get("sensor.office_studio_valve_duty") is not None
    assert hass.states.get("sensor.cooling_compressor_starts").state == "0"
