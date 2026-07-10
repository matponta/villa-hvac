"""Tests for #5 VMC boost (night free-cooling ventilation)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN, OUTDOOR_TEMP
from custom_components.villa_hvac.supervisor import vmc_boost_decision

GROUND = "switch.10_5_150_27_boost"
LIVING = "switch.vmc_boost"


# --- pure decision -----------------------------------------------------------

def _dec(**kw):
    base = dict(
        is_summer=True, outdoor=20.0, indoor=25.0, on_now=False,
        outdoor_max=24.0, margin=2.0, hysteresis=0.5,
    )
    base.update(kw)
    return vmc_boost_decision(**base)


def test_boost_when_cool_enough_outside():
    assert _dec(outdoor=20.0, indoor=25.0) is True   # 5 °C gap >= 2


def test_no_boost_out_of_season():
    assert _dec(is_summer=False) is False


def test_no_boost_when_outside_not_cool():
    assert _dec(outdoor=24.0) is False               # at the cap
    assert _dec(outdoor=25.0) is False               # above the cap


def test_no_boost_when_gap_too_small():
    assert _dec(outdoor=23.5, indoor=25.0, on_now=False) is False  # 1.5 < 2


def test_hysteresis_keeps_boost_on():
    # already on: need shrinks to 1.5 -> a 1.5 gap still boosts, 1.4 stops
    assert _dec(outdoor=23.5, indoor=25.0, on_now=True) is True
    assert _dec(outdoor=23.6, indoor=25.0, on_now=True) is False


def test_unknown_temps_never_boost():
    assert _dec(outdoor=None) is False
    assert _dec(indoor=None) is False


# --- controller (edge-triggered, deploy-dark, release) -----------------------

async def _setup(hass):
    hass.states.async_set("climate.salotto_termostato_2", "cool",
                          {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_deploy_dark_master_off_no_writes(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    on = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set("switch.vmc_auto", "on")            # opt-in on
    hass.states.async_set(OUTDOOR_TEMP, "18")                 # cool outside
    coordinator.data = {"zone_temps": {"palestra": {"value": 26.0}}}

    await coordinator.vmc._evaluate()                          # master still OFF

    assert not [c for c in on if c.data.get("entity_id") in (GROUND, LIVING)]


async def test_boost_edge_and_release(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    vmc = coordinator.vmc
    # mock AFTER setup so the switch platform's real turn_on doesn't override it
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("switch.supervisor", "on")          # master on
    hass.states.async_set("switch.vmc_auto", "on")            # opt-in on
    hass.states.async_set(OUTDOOR_TEMP, "18")                 # cool outside
    coordinator.data = {"zone_temps": {
        "palestra": {"value": 26.0},   # ground group warm
        "kitchen": {"value": 26.0},    # living group warm
    }}

    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in on)
    assert any(c.data["entity_id"] == LIVING for c in on)

    # no NEW write on a second identical evaluate (edge-triggered, no re-assert)
    n_on = len(on)
    await vmc._evaluate()
    assert len(on) == n_on

    # outside warms above the room -> release what we set
    hass.states.async_set(OUTDOOR_TEMP, "30")
    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in off)
    assert any(c.data["entity_id"] == LIVING for c in off)


async def test_disable_releases(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    vmc = coordinator.vmc
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.vmc_auto", "on")
    hass.states.async_set(OUTDOOR_TEMP, "18")
    coordinator.data = {"zone_temps": {"palestra": {"value": 26.0}}}
    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in on)

    # turning the opt-in off -> the next evaluate hands the boost back
    hass.states.async_set("switch.vmc_auto", "off")
    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in off)
