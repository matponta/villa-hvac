"""Tests for the Supervisor engine wiring (Phase A2).

The pure arbiter is covered in test_supervisor.py; here we check the HA-facing
shell: the master enable gate (deploy-dark), the reconcile-driven write path,
the fail-safe, and the house-state builder.
"""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import CONSENSO_BLOCCO, DOMAIN
from custom_components.villa_hvac.engine import SupervisorEngine, build_house_state
from custom_components.villa_hvac.supervisor import BLOCCO_LEVER, preset_lever

CLIMATE = "climate.salotto_termostato_2"
PADRONALE_VALVE = "binary_sensor.fancoil_camera_padronale_valvola"


async def _setup(hass):
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_master_switch_defaults_off_and_gates_engine(hass):
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    assert hass.states.get("switch.supervisor").state == "off"
    assert engine.enabled is False

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.supervisor"}, blocking=True,
    )
    assert engine.enabled is True


async def test_run_writes_desired_preset_via_arbiter(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    # A dummy policy that wants the Salotto in economy (currently comfort).
    def policy(_state):
        return {preset_lever(CLIMATE): "economy"}

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    await engine._run()

    assert len(calls) == 1
    assert calls[0].data["entity_id"] == CLIMATE
    assert calls[0].data["preset_mode"] == "economy"


async def test_run_is_idempotent_when_already_satisfied(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    def policy(_state):
        return {preset_lever(CLIMATE): "comfort"}  # already in comfort

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    await engine._run()
    assert len(calls) == 0  # satisfied -> no write


async def test_fail_safe_releases_blocco_when_blocking(hass):
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(CONSENSO_BLOCCO, "on")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    await engine.async_fail_safe()

    assert len(off_calls) == 1
    assert off_calls[0].data["entity_id"] == CONSENSO_BLOCCO


async def test_fail_safe_noop_when_already_released(hass):
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    await engine.async_fail_safe()
    assert len(off_calls) == 0


async def test_blocco_lever_round_trips_through_engine(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    def policy(_state):
        return {BLOCCO_LEVER: "on"}  # an envelope-rest policy wants to block

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    await engine._run()

    assert len(on_calls) == 1
    assert on_calls[0].data["entity_id"] == CONSENSO_BLOCCO


async def test_preset_policies_drive_writes_through_engine(hass):
    """End-to-end proof: the real preset policies, merged + reconciled by the
    engine, drive a preset write (validates the A4 cutover path)."""
    from custom_components.villa_hvac.policies import PRESET_POLICIES

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    # Salotto currently in standby; house mode default Casa wants comfort.
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "standby"})
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    engine = SupervisorEngine(hass, entry, coordinator, policies=PRESET_POLICIES)
    await engine._run()

    # Only Salotto has a live climate state here; other zones read None ->
    # transient -> no write. So exactly one preset write: Salotto -> comfort.
    salotto = [c for c in calls if c.data["entity_id"] == CLIMATE]
    assert len(salotto) == 1
    assert salotto[0].data["preset_mode"] == "comfort"


async def test_free_cool_suppresses_fancoil_via_engine(hass):
    """#5: summer + cool outside -> the engine forces a cooling zone to BP."""
    from custom_components.villa_hvac.policies import PRESET_POLICIES

    entry = await _setup(hass)  # salotto 'cool' -> season summer
    coordinator = entry.runtime_data
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "20.0")  # < 22
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    engine = SupervisorEngine(hass, entry, coordinator, policies=PRESET_POLICIES)
    await engine._run()

    salotto = [c for c in calls if c.data["entity_id"] == CLIMATE]
    assert salotto and salotto[-1].data["preset_mode"] == "building_protection"


async def test_shading_resolver_and_close_via_engine(hass):
    """#6: a west-labeled cover device is resolved from the registries; with the
    sun in the west and bright, the engine closes it. An orphan (no area) is
    excluded."""
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    from custom_components.villa_hvac.engine import shadeable_covers
    from custom_components.villa_hvac.policies import POLICIES

    entry = await _setup(hass)  # salotto 'cool' -> summer
    coordinator = entry.runtime_data
    area_reg, dev_reg, ent_reg = ar.async_get(hass), dr.async_get(hass), er.async_get(hass)

    area = area_reg.async_get_or_create("Test West Room")
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("knx", "shade-test")}
    )
    dev_reg.async_update_device(device.id, area_id=area.id, labels={"west"})
    cover = ent_reg.async_get_or_create("cover", "knx", "shade-uid", device_id=device.id)
    orphan = ent_reg.async_get_or_create("cover", "knx", "orphan-uid")  # no area

    resolved = {c.entity_id: c.orientation for c in shadeable_covers(hass)}
    assert resolved.get(cover.entity_id) == "west"
    assert orphan.entity_id not in resolved  # unassigned area -> skipped

    hass.states.async_set(cover.entity_id, "open")
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 260.0, "elevation": 30.0}
    )
    hass.states.async_set("sensor.gw3000a_solar_radiation", "500")
    closes = async_mock_service(hass, "cover", "close_cover")

    engine = SupervisorEngine(hass, entry, coordinator, policies=POLICIES)
    await engine._run()

    assert any(c.data["entity_id"] == cover.entity_id for c in closes)


async def test_duty_switch_defaults_off(hass):
    await _setup(hass)
    assert hass.states.get("switch.duty_cycle").state == "off"


async def test_duty_cycle_blocks_after_max_stint_via_engine(hass):
    """#9: duty on + stint exceeded -> the engine blocks via the Consenso BLOCCO."""
    from custom_components.villa_hvac.const import OPT_DUTY_COOLOFF, OPT_DUTY_MAX_STINT

    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")  # cooling
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={},
        options={OPT_DUTY_MAX_STINT: 0, OPT_DUTY_COOLOFF: 30},  # stint over at once
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Master + duty on; set states directly so we don't intercept switch.turn_on.
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.duty_cycle", "on")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await entry.runtime_data.engine.request_run()

    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in on_calls)


async def test_fan_pacing_switch_defaults_off(hass):
    await _setup(hass)
    assert hass.states.get("switch.fan_pacing").state == "off"


async def test_fan_pacing_holds_manuale_and_fan_via_engine(hass):
    """#3: pacing on + room cooling & hot -> engine forces manuale on + a pull-
    down fan speed on that fancoil."""
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    hass.states.async_set("sensor.clima_salotto", "26.0")  # fused temp -> 26
    hass.states.async_set("binary_sensor.fancoil_salotto_valvola", "on")  # demand
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")  # run
    hass.states.async_set("fan.fancoil_salotto", "on", {"percentage": 0})
    hass.states.async_set("switch.fancoil_salotto_manuale", "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.fan_pacing", "on")
    fan_calls = async_mock_service(hass, "fan", "set_percentage")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await entry.runtime_data.engine.request_run()

    assert any(c.data["entity_id"] == "fan.fancoil_salotto" for c in fan_calls)
    assert any(
        c.data["entity_id"] == "switch.fancoil_salotto_manuale" for c in on_calls
    )


async def test_build_house_state_snapshot(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set(PADRONALE_VALVE, "on")

    state = build_house_state(hass, entry, coordinator)

    assert state.season == "summer"  # salotto thermostat is 'cool'
    assert state.house_mode == "Casa"  # select default
    assert "main_bedroom" in state.zones
    assert state.zones["main_bedroom"].demand is True  # valve open
    assert state.zones["living_room"].enabled is True  # #10 default on
    assert state.zones["living_room"].climate == CLIMATE
