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

    # BLOCCO released; fancoil manuale switches are also released unconditionally.
    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_fail_safe_releases_blocco_even_when_read_off(hass):
    """Fail-open: the block is released UNCONDITIONALLY, never gated on the read.
    A transient unavailable/off read must not skip the release (idempotent)."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    await engine.async_fail_safe()
    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_release_blocco_is_unconditional(hass):
    """async_release_blocco always sends turn_off (safe baseline), even when the
    switch reads unavailable — the read is never trusted for the safety lever."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(CONSENSO_BLOCCO, "unavailable")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    await engine.async_release_blocco()
    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_shutdown_event_triggers_failsafe(hass):
    """HA shutdown (EVENT_HOMEASSISTANT_STOP) releases a live block — unload
    callbacks do not run on shutdown, so this hook is the guarantee."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    await _setup(hass)
    hass.states.async_set(CONSENSO_BLOCCO, "on")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_master_off_triggers_failsafe(hass):
    """Turning the master Supervisor switch off hands the villa back (fail-safe),
    so a live block is never stranded with the engine idle."""
    from unittest.mock import AsyncMock

    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    engine.async_fail_safe = AsyncMock()

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.supervisor"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.supervisor"}, blocking=True
    )
    await hass.async_block_till_done()

    engine.async_fail_safe.assert_awaited()


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


async def test_shading_resolver_and_position_via_engine(hass):
    """#6: a west-labeled cover device is resolved from the registries; with the
    sun in the west and bright, the engine drives it to the default shade
    position (not a full close). An orphan (no area) is excluded."""
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

    # fully open now (position 100); shading should drive it to the default 50.
    hass.states.async_set(cover.entity_id, "open", {"current_position": 100})
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 260.0, "elevation": 30.0}
    )
    hass.states.async_set("sensor.gw3000a_solar_radiation", "500")
    positions = async_mock_service(hass, "cover", "set_cover_position")

    engine = SupervisorEngine(hass, entry, coordinator, policies=POLICIES)
    await engine._run()

    sets = [c for c in positions if c.data["entity_id"] == cover.entity_id]
    assert sets and sets[-1].data["position"] == 50  # DEFAULT_SHADING_POSITION


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


async def test_forecast_precool_nudges_setpoint_via_engine(hass):
    """#9 planner: a hot forecast within the lead window -> the engine pre-cools
    (nudges the fancoil setpoint colder)."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )  # summer; current setpoint 24
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "25.0")  # cool now
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.duty_cycle", "on")  # pre-cool is gated by #9
    engine = entry.runtime_data.engine
    # Inject a hot forecast + a fresh timestamp so the real fetch is skipped.
    engine._forecast = [(dt_util.utcnow() + timedelta(hours=1), 33.0)]
    engine._forecast_ts = dt_util.utcnow()
    temps = async_mock_service(hass, "climate", "set_temperature")

    await engine.request_run()

    salotto = [c for c in temps if c.data["entity_id"] == CLIMATE]
    assert salotto and salotto[-1].data["temperature"] == 22.5  # 24 - 1.5 precool


async def test_shade_controls_enrich_cover_state(hass):
    """#6: the per-room shade-position number + shade-block switch are read back
    into each cover's snapshot (keyed by the cover's area_id)."""
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    area_reg, dev_reg, ent_reg = ar.async_get(hass), dr.async_get(hass), er.async_get(hass)

    area = area_reg.async_get_or_create("South Room")
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("knx", "shade2")}
    )
    dev_reg.async_update_device(device.id, area_id=area.id, labels={"south"})
    cover = ent_reg.async_get_or_create("cover", "knx", "shade2-uid", device_id=device.id)
    num = ent_reg.async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_shade_position_{area.id}"
    )
    sw = ent_reg.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_shade_block_{area.id}"
    )
    hass.states.async_set(num.entity_id, "30")
    hass.states.async_set(sw.entity_id, "on")

    state = build_house_state(hass, entry, coordinator)

    ci = next(c for c in state.covers if c.entity_id == cover.entity_id)
    assert ci.target_position == 30 and ci.blocked is True


async def test_estimator_observes_even_deploy_dark(hass):
    """F2: the thermal observer learns every cycle even with the master OFF, so
    passive params converge before actuation lights up."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    assert engine.enabled is False  # deploy-dark
    await engine._cycle(actuate=False)
    # the observer seeded the cooling-fancoil leaders' models read-only.
    assert "living_room" in engine.thermal.params
    assert "main_bedroom" in engine.thermal.params


async def test_model_injected_into_snapshot(hass):
    """F2: build_house_state surfaces the blended model on leader ZoneSnapshots
    (prior values until a room converges)."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    state = build_house_state(hass, entry, coordinator)
    lr = state.zones["living_room"]
    assert lr.model_a is not None and lr.model_k is not None  # prior, blended in
    assert lr.model_k_confidence == 0.0  # nothing learned yet


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


async def test_num_rejects_nan_and_inf(hass):
    """_num returns None for non-finite reads so NaN/inf can't poison the
    at-peak / free-cool comparisons downstream (ENGINE_REVIEW §5)."""
    from custom_components.villa_hvac.engine import _num

    hass.states.async_set("sensor.x", "21.5")
    assert _num(hass, "sensor.x") == 21.5
    hass.states.async_set("sensor.x", "nan")
    assert _num(hass, "sensor.x") is None
    hass.states.async_set("sensor.x", "inf")
    assert _num(hass, "sensor.x") is None


async def test_stopped_engine_does_not_actuate(hass):
    """After stop() (terminal teardown), a still-queued/in-flight cycle must not
    actuate — otherwise a late tick could re-block BLOCCO after the fail-safe."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    def policy(_state):
        return {preset_lever(CLIMATE): "economy"}

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    engine.stop()
    await engine._cycle(actuate=True)
    assert calls == []  # stopped -> no writes


async def test_request_run_queues_behind_running_cycle(hass):
    """A request_run arriving mid-cycle queues on the lock (serialised) instead
    of interleaving or being dropped."""
    import asyncio

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set("switch.supervisor", "on")  # enabled
    engine = SupervisorEngine(hass, entry, coordinator, policies=[])

    await engine._lock.acquire()  # simulate a cycle already in flight
    ran = []

    async def _fire():
        await engine.request_run()
        ran.append(True)

    task = asyncio.create_task(_fire())
    await asyncio.sleep(0.05)
    assert ran == []  # blocked on the lock, not dropped

    engine._lock.release()
    await task
    assert ran == [True]  # ran after the lock freed


async def test_blocco_release_reasserts_forever_through_engine(hass):
    """The BLOCCO lever is wired with allow_override=False: a stuck 'on' read is
    re-asserted 'off' EVERY cycle, never conceded to a phantom manual change."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set(CONSENSO_BLOCCO, "on")  # physically stuck (mock won't move it)
    off_calls = async_mock_service(hass, "switch", "turn_off")

    def policy(_state):
        return {BLOCCO_LEVER: "off"}

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    for _ in range(6):  # well past max_reasserts (3)
        await engine._run()

    blocco = [c for c in off_calls if c.data["entity_id"] == CONSENSO_BLOCCO]
    assert len(blocco) == 6  # re-asserted every cycle, never gave up
    assert engine._lever_states[BLOCCO_LEVER].override_until is None


async def test_duty_off_releases_a_blocked_villa_via_engine(hass):
    """Duty disabled + a live block -> the engine actively clears it in one pass
    (DutyController emits an explicit BLOCCO_RELEASE, not a silent {})."""
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    hass.states.async_set(CONSENSO_BLOCCO, "on")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")  # master on, duty OFF (default)
    off_calls = async_mock_service(hass, "switch", "turn_off")

    await entry.runtime_data.engine.request_run()

    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_startup_resync_releases_stranded_blocco(hass):
    """On HA start (KNX up), the boot safe-baseline releases a block stranded
    across a crash/restart, regardless of the (off) master switch."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    await _setup(hass)
    hass.states.async_set(CONSENSO_BLOCCO, "on")
    off_calls = async_mock_service(hass, "switch", "turn_off")

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)


async def test_startup_resync_unsub_idempotent_on_unload(hass, caplog):
    """Once the HOMEASSISTANT_STARTED listener fires it auto-removes itself, so
    unloading afterwards must NOT unsubscribe it a second time — HA logs an
    ERROR ('unknown job listener') for that, seen live after a reload."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    entry = await _setup(hass)
    async_mock_service(hass, "switch", "turn_off")  # absorb the fail-safe release

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)  # listener fires + self-removes
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert "unknown job listener" not in caplog.text


async def test_queued_cycle_aborts_if_stopped_while_waiting(hass):
    """A cycle queued behind the lock must NOT actuate if the engine is stopped
    while it waits — the post-acquire _stopped re-check (teardown ordering)."""
    import asyncio

    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set("switch.supervisor", "on")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    def policy(_state):
        return {preset_lever(CLIMATE): "economy"}

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    await engine._lock.acquire()  # a cycle is "in flight"
    ran = []

    async def _fire():
        await engine.request_run()  # queues on the lock (not yet stopped)
        ran.append(True)

    task = asyncio.create_task(_fire())
    await asyncio.sleep(0.05)
    engine.stop()             # teardown while the pass is queued behind the lock
    engine._lock.release()
    await task

    assert calls == []        # queued cycle saw _stopped after acquiring -> no write
    assert ran == [True]


async def test_coordinator_rejects_nan_fused_temp(hass):
    """A 'nan' from the primary temp sensor must not surface as a fused zone temp
    (the coordinator isfinite guard); it falls back to None here."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.states.async_set("sensor.clima_salotto", "nan")
    await coordinator.async_refresh()

    state = build_house_state(hass, entry, coordinator)
    assert state.zones["living_room"].temp is None
