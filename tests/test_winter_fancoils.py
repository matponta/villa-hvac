"""v0.79.0 — winter: every fancoil fan held OFF (radiant only), handed back on summer."""
from __future__ import annotations

from datetime import datetime, timezone

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    DOMAIN,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
    UNIT_FANS,
)
from custom_components.villa_hvac.supervisor import HouseState, fan_power_lever
from custom_components.villa_hvac.winter import WINTER_FANS, WinterFancoilController

NOW = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


def _st(season):
    return HouseState(now=NOW, zones={}, season=season)


def test_all_live_fancoil_fans_are_covered():
    from custom_components.villa_hvac.const import DEAD_FANCOILS

    assert set(WINTER_FANS) == set(UNIT_FANS.values()) - DEAD_FANCOILS
    assert len(WINTER_FANS) == 7


def test_winter_holds_off_then_hands_back_once_in_summer():
    c = WinterFancoilController()
    assert c(_st(SEASON_SUMMER)) == {}                      # never managed: silent
    out = c(_st(SEASON_WINTER))
    assert out == {fan_power_lever(f): "off" for f in WINTER_FANS}
    assert c(_st(SEASON_WINTER)) == out                     # held every cycle
    from custom_components.villa_hvac.winter import HANDBACK_CYCLES

    on = {fan_power_lever(f): "on" for f in WINTER_FANS}
    for _ in range(HANDBACK_CYCLES):                        # asserted for a window
        assert c(_st(SEASON_SUMMER)) == on                  # (a lost telegram is
    assert c(_st(SEASON_SUMMER)) == {}                      #  re-asserted), then free


async def test_engine_turns_an_on_at_zero_fan_off_in_winter(hass):
    """Live 2026-10-07: after the changeover every fan sat ON at 0 % in AUTO —
    the % lever reads that as satisfied; the fan_power lever must write OFF."""
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    for f in WINTER_FANS:
        hass.states.async_set(f, "on", {"percentage": 0})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")
    offs = async_mock_service(hass, "fan", "turn_off")
    ons = async_mock_service(hass, "fan", "turn_on")
    engine = entry.runtime_data.engine
    engine._fans_turned_off.add("fan.fancoil_camera_padronale")  # owed from summer
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.supervisor"}, blocking=True
    )
    await engine.request_run()
    await hass.async_block_till_done()
    assert {c.data["entity_id"] for c in offs} == set(WINTER_FANS)
    assert "fan.fancoil_sala_giochi" not in {c.data["entity_id"] for c in offs}
    assert not ons
    # no summer dead-fan re-arm owed in winter -> the fail-safe won't spin them up
    assert engine._fans_turned_off == set()
