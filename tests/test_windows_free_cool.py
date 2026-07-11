"""Tests for the windows → free-cooling inference (owner rule 2, v0.56.0).

Enough window CONTACTS open + outdoor meaningfully cooler than the house
indoor mean (+ the opt-in switch + summer) → `windows_free_cool` on the
HouseState, ORed into `_is_free_cooling` so the whole #5 coast stack follows.
"""
from __future__ import annotations

from dataclasses import replace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.engine import build_house_state

from .helpers import seed_thermostats

CONTACTS = (
    "binary_sensor.main_bedroom_finestra_piccola_bedroom_window",
    "binary_sensor.gabri_room_finestra_g_window",
    "binary_sensor.aaa_window",                       # studio_v
)
OUTDOOR = "sensor.gw3000a_outdoor_temperature"
SWITCH = "switch.windows_free_cooling"


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _seed(hass, entry, *, contacts=CONTACTS, outdoor="22.0", switch_on=True):
    seed_thermostats(hass, preset="comfort")          # salotto 'cool' -> summer
    hass.states.async_set(OUTDOOR, outdoor)
    hass.states.async_set("sensor.clima_salotto", "26.0")     # leader fused temps
    hass.states.async_set("sensor.clima_sala_giochi", "26.0")  # indoor mean = 26
    for c in contacts:
        hass.states.async_set(c, "on")
    await entry.runtime_data.async_refresh()
    if switch_on:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
        )
        await hass.async_block_till_done()


def _build_aged(hass, entry):
    """Two builds with the entry-dwell anchor backdated: the verdict engages
    only after WINDOWS_FREE_COOL_DWELL of sustained raw conditions (a 30 s door
    transit must never slam the whole house — adversarial review)."""
    from custom_components.villa_hvac.const import WINDOWS_FREE_COOL_DWELL

    build_house_state(hass, entry, entry.runtime_data)   # starts the dwell
    eng = entry.runtime_data.engine
    if eng.windows_fc_since is not None:
        eng.windows_fc_since -= WINDOWS_FREE_COOL_DWELL
    return build_house_state(hass, entry, entry.runtime_data)


async def test_airing_verdict_and_coast(hass):
    """3 contacts open + outdoor 22 vs indoor mean 26 + switch on, SUSTAINED
    past the dwell -> verdict on, and _is_free_cooling coasts even with the #5
    outdoor switch OFF."""
    from custom_components.villa_hvac.supervisor import _is_free_cooling

    entry = await _setup(hass)
    await _seed(hass, entry)

    state = _build_aged(hass, entry)
    assert set(state.windows_open) == {"main_bedroom", "gabriroom", "studio_v"}
    assert state.windows_free_cool is True
    assert state.free_cool_enabled is False           # #5 switch untouched
    assert _is_free_cooling(state) is True


async def test_airing_bps_a_zone_without_its_own_window(hass):
    """The verdict must coast the WHOLE house: free_cool_policy BPs a cooled
    zone that has no open window of its own (sala_giochi)."""
    from custom_components.villa_hvac.policies import free_cool_policy

    entry = await _setup(hass)
    await _seed(hass, entry)

    state = _build_aged(hass, entry)
    out = free_cool_policy(state)
    assert (
        out.get("preset:climate.sala_giochi_termostato_2") == "building_protection"
    )


async def test_airing_requires_the_opt_in_switch(hass):
    entry = await _setup(hass)
    await _seed(hass, entry, switch_on=False)
    state = build_house_state(hass, entry, entry.runtime_data)
    assert set(state.windows_open) == {"main_bedroom", "gabriroom", "studio_v"}
    assert state.windows_free_cool is False           # observability without actuation


async def test_airing_requires_enough_windows(hass):
    entry = await _setup(hass)
    await _seed(hass, entry, contacts=CONTACTS[:2])   # 2 < default 3
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.windows_free_cool is False


async def test_airing_requires_summer(hass):
    """Winter: 3 windows open never coasts (there is no cooling to coast; the
    heating side is the per-room #4 pause + the rule-3 alert)."""
    entry = await _setup(hass)
    await _seed(hass, entry)
    seed_thermostats(hass, hvac="heat", preset="comfort")   # season -> winter
    state = _build_aged(hass, entry)
    assert state.windows_free_cool is False


async def test_airing_requires_cooler_outside(hass):
    entry = await _setup(hass)
    await _seed(hass, entry, outdoor="25.5")          # > indoor 26 − margin 1.0
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.windows_free_cool is False


async def test_airing_ignores_unavailable_contacts(hass):
    """A dead BLE battery reads unavailable -> counts as CLOSED (never a false
    coast)."""
    entry = await _setup(hass)
    await _seed(hass, entry, contacts=CONTACTS[:2])
    hass.states.async_set(CONTACTS[2], "unavailable")
    await entry.runtime_data.async_refresh()
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.windows_free_cool is False
    assert "studio_v" not in state.windows_open


async def test_entry_dwell_blocks_a_door_transit(hass):
    """A momentary count spike (kitchen-door transit) must NOT coast the house:
    the verdict engages only after the dwell, and a raw-condition drop resets
    the anchor."""
    entry = await _setup(hass)
    await _seed(hass, entry)

    state = build_house_state(hass, entry, entry.runtime_data)  # first sight
    assert state.windows_free_cool is False          # dwell not yet elapsed
    assert entry.runtime_data.engine.windows_fc_since is not None

    # one contact closes -> raw conditions drop -> anchor resets
    hass.states.async_set(CONTACTS[0], "off")
    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.windows_free_cool is False
    assert entry.runtime_data.engine.windows_fc_since is None

    # reopen + sustain past the dwell -> engages
    hass.states.async_set(CONTACTS[0], "on")
    state = _build_aged(hass, entry)
    assert state.windows_free_cool is True


async def test_verdict_survives_downstream_replace(hass):
    """The band/planner read _is_free_cooling off replaced states — the verdict
    must ride along (a frozen-field regression guard)."""
    from custom_components.villa_hvac.supervisor import _is_free_cooling

    entry = await _setup(hass)
    await _seed(hass, entry)
    state = _build_aged(hass, entry)
    assert _is_free_cooling(replace(state, pv_mode="bank")) is True


async def test_engine_coasts_then_restores_through_the_real_cycle(hass):
    """LIVE path: the aged verdict must BP a windowless cooled zone via the
    real engine cycle, and closing the windows must restore it to the house
    mode on the next pass."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.villa_hvac.const import WINDOWS_FREE_COOL_DWELL
    from .helpers import enable_supervisor

    entry = await _setup(hass)
    await _seed(hass, entry, switch_on=False)
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")
    await enable_supervisor(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine

    await engine._run()                              # starts the dwell
    engine.windows_fc_since -= WINDOWS_FREE_COOL_DWELL
    presets.clear()
    await engine._run()                              # verdict engaged -> coast
    await hass.async_block_till_done()

    giochi = [c for c in presets
              if c.data["entity_id"] == "climate.sala_giochi_termostato_2"]
    assert giochi and all(
        c.data["preset_mode"] == "building_protection" for c in giochi
    )

    # Windows close -> verdict drops immediately -> house mode restores.
    hass.states.async_set(
        "climate.sala_giochi_termostato_2", "cool",
        {"preset_mode": "building_protection", "temperature": 24.0},
    )
    for c in CONTACTS:
        hass.states.async_set(c, "off")
    presets.clear()
    await engine._run()
    await hass.async_block_till_done()

    giochi = [c for c in presets
              if c.data["entity_id"] == "climate.sala_giochi_termostato_2"]
    assert giochi and all(c.data["preset_mode"] == "comfort" for c in giochi)


async def test_feature_graph_row_states(hass):
    """The windows_free_cool feature row: disabled by default; enabled+inert
    with the reason naming the open count; active once the verdict engages."""
    from custom_components.villa_hvac.supervisor import (
        DutyState,
        build_feature_graph,
        build_plan,
        plan_run,
    )

    entry = await _setup(hass)
    await _seed(hass, entry, switch_on=False)
    state = build_house_state(hass, entry, entry.runtime_data)
    run_plan = plan_run(
        [], state.now, state.outdoor_temp,
        peak_threshold=state.config.duty_peak_outdoor,
        lookahead=state.config.lookahead,
        margin=state.config.precool_margin,
    )
    plan = build_plan(state, run_plan, {}, DutyState(), [], state.config.lookahead)

    def row(graph):
        return next(f for f in graph if f.feature == "windows_free_cool")

    g = build_feature_graph(state, plan, master_on=True, enabled={})
    assert row(g).enabled is False and row(g).inert_reason == "disabled"

    g = build_feature_graph(
        state, plan, master_on=True, enabled={"windows_free_cool": True}
    )
    assert row(g).enabled is True and row(g).active is False
    assert "3 window(s) open" in row(g).inert_reason

    aged = _build_aged(hass, entry)  # switch still off -> verdict False; flip it
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    aged = _build_aged(hass, entry)
    g = build_feature_graph(
        aged, plan, master_on=True, enabled={"windows_free_cool": True}
    )
    assert row(g).active is True and row(g).inert_reason is None
