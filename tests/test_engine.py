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
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.villa_hvac.const import OPT_DUTY_COOLOFF, OPT_DUTY_MAX_STINT
    from custom_components.villa_hvac.supervisor import DutyState

    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")  # cooling
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={},
        options={OPT_DUTY_MAX_STINT: 15, OPT_DUTY_COOLOFF: 30},  # 15 = clamp min
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Master + duty on; set states directly so we don't intercept switch.turn_on.
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.duty_cycle", "on")
    on_calls = async_mock_service(hass, "switch", "turn_on")
    # Force the stint to already exceed the (clamped) 15-min cap. (M1: the duty
    # state lives on the folded CoolingController; field name _duty preserved.)
    engine = entry.runtime_data.engine
    engine._cooling._duty = DutyState(stint_start=dt_util.utcnow() - timedelta(minutes=20))

    await engine.request_run()

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


# --- B3: full policies + controllers stack through _cycle (merge-order seam) --

async def test_band_setpoint_beats_house_mode_via_engine(hass):
    """B3 seam: controllers merge BEFORE the pure policies, so the #3 band setpoint
    wins over house_mode on a leader it actively manages (locks [*ctrl, *pure])."""
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )  # summer; house_setpoint default 24 + Casa summer offset 0 -> center 24
    hass.states.async_set("sensor.clima_salotto", "26.0")            # warm -> RUN
    hass.states.async_set("binary_sensor.fancoil_salotto_valvola", "on")
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")
    hass.states.async_set("fan.fancoil_salotto", "on", {"percentage": 0})
    hass.states.async_set("switch.fancoil_salotto_manuale", "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.fan_pacing", "on")
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "set_percentage")
    async_mock_service(hass, "switch", "turn_on")

    await entry.runtime_data.engine.request_run()

    salotto = [c for c in temps if c.data["entity_id"] == CLIMATE]
    # band RUN slam = center(24) - A(0.75) = 23.25; house_mode wanted 24 (== current
    # -> no write). A 23.25 write can only be the band winning the controllers-first
    # merge over house_mode.
    assert salotto and salotto[-1].data["temperature"] == 23.25


async def test_free_cool_forces_bp_and_band_yields_via_engine(hass):
    """B3 seam: with #5 free-cool active the band YIELDS on that leader and
    free_cool forces building_protection — no setpoint is pushed onto the BP zone."""
    from custom_components.villa_hvac.const import PRESET_BUILDING_PROTECTION

    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )
    hass.states.async_set("sensor.clima_salotto", "26.0")
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "20.0")  # < 22 -> free
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.fan_pacing", "on")  # band enabled, but must yield
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "switch", "turn_off")

    await entry.runtime_data.engine.request_run()

    assert any(
        c.data["entity_id"] == CLIMATE
        and c.data["preset_mode"] == PRESET_BUILDING_PROTECTION
        for c in presets
    )  # #5 forced BP
    assert not [c for c in temps if c.data["entity_id"] == CLIMATE]  # band yielded


async def test_regime_release_beats_duty_block_via_engine(hass):
    """B3 seam (M1: now INSIDE CoolingController.__call__): the coalescing
    BLOCCO opinion takes precedence over the duty one, so a coalescing RELEASE
    overrides a duty BLOCK. (The regime derivation itself is covered by the pure
    select_regime tests; here we lock only the composed precedence.)"""
    from custom_components.villa_hvac.const import OPT_DUTY_COOLOFF, OPT_DUTY_MAX_STINT
    from custom_components.villa_hvac.supervisor import BLOCCO_RELEASE

    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")  # cooling
    hass.states.async_set(CONSENSO_BLOCCO, "on")  # currently BLOCKED
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={},
        options={OPT_DUTY_MAX_STINT: 0, OPT_DUTY_COOLOFF: 30},  # duty wants to BLOCK
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.duty_cycle", "on")
    engine = entry.runtime_data.engine
    # force a coalescing RELEASE opinion
    engine._cooling.regime_pass = lambda state: ({}, BLOCCO_RELEASE)
    off_calls = async_mock_service(hass, "switch", "turn_off")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await engine.request_run()

    # regime RELEASE beat duty BLOCK -> the standing block is cleared, never re-set.
    assert any(c.data["entity_id"] == CONSENSO_BLOCCO for c in off_calls)
    assert not any(c.data["entity_id"] == CONSENSO_BLOCCO for c in on_calls)


async def test_lever_decisions_recorded_via_engine(hass):
    """B2: the engine records each lever's reconcile decision (sensor.hvac_levers),
    and reports no manual-held levers on a clean run."""
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "standby", "temperature": 25.0}
    )  # summer; house_mode Casa wants comfort -> a real preset write to record
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")
    engine = entry.runtime_data.engine

    await engine.request_run()

    decisions = engine.lever_decisions
    assert decisions[preset_lever(CLIMATE)]["note"] == "write"  # standby -> comfort
    assert decisions[preset_lever(CLIMATE)]["desired"] == "comfort"
    held = [k for k, d in decisions.items() if d["note"] in ("override", "manual-hold")]
    assert held == []  # supervisor fully in control


# --- B1: fail-safe restores per-zone presets (un-stick lingering BP) ----------

async def test_failsafe_restores_bp_zone_to_auto(hass):
    """B1: a zone the supervisor slammed to building_protection is handed back to
    the neutral `auto` preset on fail-safe (never left unable to condition)."""
    from custom_components.villa_hvac.const import PRESET_BUILDING_PROTECTION

    entry = await _setup(hass)  # living_room (salotto) = fancoil leader, enabled
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": PRESET_BUILDING_PROTECTION})
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "switch", "turn_off")  # absorb BLOCCO/manuale release

    await entry.runtime_data.engine.async_fail_safe()

    restored = [c for c in presets if c.data["entity_id"] == CLIMATE]
    assert restored and restored[-1].data["preset_mode"] == "auto"


async def test_failsafe_leaves_non_bp_preset_untouched(hass):
    """B1: only a LINGERING building_protection is un-stuck; a zone in comfort is
    left alone (no gratuitous preset churn)."""
    entry = await _setup(hass)
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "switch", "turn_off")

    await entry.runtime_data.engine.async_fail_safe()

    assert not [c for c in presets if c.data["entity_id"] == CLIMATE]


async def test_failsafe_skips_disabled_zone(hass):
    """B1: a #10-disabled zone SHOULD stay in building_protection — the fail-safe
    must not re-enable it."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.villa_hvac.const import PRESET_BUILDING_PROTECTION

    entry = await _setup(hass)
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": PRESET_BUILDING_PROTECTION})
    enable = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_living_room_enabled"
    )
    assert enable is not None  # living_room owns a #10 enable switch
    hass.states.async_set(enable, "off")  # zone disabled -> keep BP
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "switch", "turn_off")

    await entry.runtime_data.engine.async_fail_safe()

    assert not [c for c in presets if c.data["entity_id"] == CLIMATE]


async def test_failsafe_skips_window_paused_zone(hass):
    """B1: a window-paused zone SHOULD stay in building_protection too."""
    from custom_components.villa_hvac.const import PRESET_BUILDING_PROTECTION

    entry = await _setup(hass)
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": PRESET_BUILDING_PROTECTION})
    entry.runtime_data.window.paused.add("living_room")  # #4 pause
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "switch", "turn_off")

    await entry.runtime_data.engine.async_fail_safe()

    assert not [c for c in presets if c.data["entity_id"] == CLIMATE]


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


async def test_queued_cycle_aborts_after_failsafe_handback(hass):
    """A cycle queued behind an in-flight fail-safe must NOT actuate after the
    hand-back — even with the master ON (where `_stopped` is never set). The epoch
    bump invalidates the stale queued pass, so it can't re-block BLOCCO with the
    master off and nothing left to clear it (DEFECT-1 / master-off strand)."""
    import asyncio

    from custom_components.villa_hvac.const import OPT_DUTY_COOLOFF, OPT_DUTY_MAX_STINT

    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})  # summer
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")  # cooling
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={},
        options={OPT_DUTY_MAX_STINT: 0, OPT_DUTY_COOLOFF: 30},  # a live cycle WOULD block
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")   # master ON
    hass.states.async_set("switch.duty_cycle", "on")
    engine = entry.runtime_data.engine
    on_calls = async_mock_service(hass, "switch", "turn_on")   # a re-block = turn_on
    async_mock_service(hass, "switch", "turn_off")             # absorb fail-safe release

    await engine._lock.acquire()            # simulate an in-flight cycle holding the lock
    queued = asyncio.create_task(engine._cycle(actuate=True))  # captures the old epoch
    await asyncio.sleep(0.05)               # let it block on lock.acquire
    fs = asyncio.create_task(engine.async_fail_safe())  # bumps epoch, then wants the lock
    await asyncio.sleep(0.05)
    engine._lock.release()                  # in-flight cycle "finishes"
    await asyncio.gather(queued, fs)

    # the queued cycle saw the epoch change -> aborted -> never re-blocked BLOCCO.
    assert not any(c.data["entity_id"] == CONSENSO_BLOCCO for c in on_calls)


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


# --- Phase 0: input hardening (B4 / C5) --------------------------------------

async def test_forecast_and_cloud_sorted_after_refresh(hass):
    """B4: `_forecast`/`_cloud` must end up time-sorted even if the weather
    integration returns hourly entries out of order (the *_at scans early-break
    assuming ascending time)."""
    from datetime import timedelta

    from homeassistant.core import SupportsResponse
    from homeassistant.util import dt as dt_util

    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set("weather.forecast_home", "sunny")
    now = dt_util.utcnow()
    t1 = (now + timedelta(hours=1)).isoformat()
    t2 = (now + timedelta(hours=2)).isoformat()
    t3 = (now + timedelta(hours=3)).isoformat()

    async def _fake_forecast(_call):
        # deliberately OUT OF ORDER (t3, t1, t2)
        return {
            "weather.forecast_home": {
                "forecast": [
                    {"datetime": t3, "temperature": 33, "cloud_coverage": 30},
                    {"datetime": t1, "temperature": 28, "cloud_coverage": 10},
                    {"datetime": t2, "temperature": 31, "cloud_coverage": 20},
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", _fake_forecast,
        supports_response=SupportsResponse.ONLY,
    )
    engine._forecast_ts = None  # force a refresh
    await engine._maybe_refresh_forecast()

    ftimes = [w for w, _ in engine._forecast]
    ctimes = [w for w, _ in engine._cloud]
    assert ftimes == sorted(ftimes)
    assert ctimes == sorted(ctimes)
    assert [t for _, t in engine._forecast] == [28.0, 31.0, 33.0]  # ordered by time


async def test_cover_read_derives_position_from_open_closed(hass):
    """B4: a cover with no `current_position` attribute must still yield a numeric
    read (0 = closed, 100 = open) so the reconcile can compare it to a target."""
    entry = await _setup(hass)
    engine = SupervisorEngine(hass, entry, entry.runtime_data)

    hass.states.async_set("cover.x", "closed", {})
    assert engine._read_current("cover:cover.x") == 0
    hass.states.async_set("cover.x", "open", {})
    assert engine._read_current("cover:cover.x") == 100
    hass.states.async_set("cover.x", "open", {"current_position": 42})
    assert engine._read_current("cover:cover.x") == 42
    hass.states.async_set("cover.x", "unknown", {})
    assert engine._read_current("cover:cover.x") is None  # transient -> None


async def test_shadeable_covers_cached_and_invalidated(hass):
    """C5: the shadeable-cover resolution is cached (no full-registry scan every
    cycle) and invalidated on a registry-updated event."""
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    entry = await _setup(hass)
    area_reg, dev_reg, ent_reg = ar.async_get(hass), dr.async_get(hass), er.async_get(hass)
    area = area_reg.async_get_or_create("Cache West Room")
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("knx", "cache-test")}
    )
    dev_reg.async_update_device(device.id, area_id=area.id, labels={"west"})
    ent_reg.async_get_or_create("cover", "knx", "cache-uid", device_id=device.id)

    engine = SupervisorEngine(hass, entry, entry.runtime_data)
    engine.start()
    try:
        c1 = engine._resolve_covers()
        assert c1  # non-empty (a west cover exists)
        assert engine._resolve_covers() is c1  # cached: same object
        assert len(engine._reg_unsubs) == 3    # entity/device/area listeners wired

        hass.bus.async_fire(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            {"action": "update", "entity_id": "cover.whatever"},
        )
        await hass.async_block_till_done()
        assert engine._covers_cache is None     # listener invalidated the cache
        assert engine._resolve_covers() is not c1  # re-resolved -> new object
    finally:
        engine.stop()
    assert engine._reg_unsubs == []


async def test_stale_temp_warns_after_threshold(hass):
    """B4: a controlled cooling leader with no fused temp for STALE_TEMP_CYCLES
    cycles is surfaced (and warned once); a returning temp clears it."""
    from dataclasses import replace

    from homeassistant.util import dt as dt_util

    from custom_components.villa_hvac.const import STALE_TEMP_CYCLES
    from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot

    entry = await _setup(hass)
    engine = SupervisorEngine(hass, entry, entry.runtime_data)
    leader = ZoneSnapshot(
        zone_id="living_room", name="lr", climate="climate.x", emitter="fancoil",
        fancoil_units=(("fan.x", "switch.x_man"),), temp=None, enabled=True,
    )
    state = HouseState(now=dt_util.utcnow(), zones={"living_room": leader})

    for _ in range(STALE_TEMP_CYCLES - 1):
        engine._track_stale_temp(state)
    assert engine.stale_temp_leaders == []          # not yet at the threshold
    engine._track_stale_temp(state)
    assert engine.stale_temp_leaders == ["living_room"]

    warm = replace(state, zones={"living_room": replace(leader, temp=24.0)})
    engine._track_stale_temp(warm)
    assert engine.stale_temp_leaders == []          # a returning temp clears it


async def test_lever_call_timeout_is_swallowed(hass, monkeypatch):
    """C5: a wedged KNX write must not stall the cycle — `_call` times out and
    returns instead of hanging/raising."""
    import asyncio

    import custom_components.villa_hvac.engine as engine_mod

    entry = await _setup(hass)
    engine = SupervisorEngine(hass, entry, entry.runtime_data)
    monkeypatch.setattr(engine_mod, "LEVER_CALL_TIMEOUT", 0.05)

    async def _slow(_call):
        await asyncio.sleep(5)

    hass.services.async_register("climate", "set_preset_mode", _slow)
    # Must return (not raise, not hang) despite the 5 s handler + 0.05 s timeout.
    await engine._call(
        "climate", "set_preset_mode",
        {"entity_id": "climate.x", "preset_mode": "comfort"},
    )


async def test_plan_view_surfaces_center_compositions(hass):
    """F4c Phase 1 observability: the plan view exposes each cooling leader's
    composed band center (computed read-only every cycle, even deploy-dark)."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )  # summer
    hass.states.async_set("sensor.clima_salotto", "25.0")
    await engine._tick()  # deploy-dark tick still computes the plan view

    comps = engine.plan_view.center_compositions
    assert "living_room" in comps
    # Casa (offset 0) + default setpoint 24, no features -> base center 24.
    assert comps["living_room"]["source"] == "base"
    assert comps["living_room"]["center"] == 24.0


async def test_plan_view_surfaces_center_schedule(hass):
    """F4c Phase 5: the plan view exposes the unified band-center REFERENCE schedule
    (PLAN-ONLY, computed read-only every cycle even deploy-dark)."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )  # summer
    hass.states.async_set("sensor.clima_salotto", "24.0")
    await entry.runtime_data.async_refresh()  # fuse the zone temp
    await engine._tick()  # deploy-dark tick still builds the schedule

    sched = engine.plan_view.center_schedule
    assert sched is not None and sched.created_at is not None
    assert sched.house_blocco == "off"          # the reference never blocks
    assert "living_room" in sched.zones          # a cooling leader is scheduled
    zs = sched.zones["living_room"]
    assert zs.points and zs.points[0].center == 24.0  # base center (no features)


async def test_unified_planner_switch_defaults_off(hass):
    await _setup(hass)
    assert hass.states.get("switch.unified_planner").state == "off"


async def test_failsafe_mid_loop_epoch_bump_stops_lever_writes(hass):
    """(P2 hardening) A fail-safe firing while the reconcile loop is mid-flight
    (epoch bumped after its bounded lock wait expired) must stop any FURTHER
    lever writes from that stale cycle — a wedged KNX write (10 s lever timeout)
    can outlive the fail-safe's 5 s lock wait, and the resuming loop must not
    chase the hand-back (a re-asserted block on master-off would never clear)."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    second = "climate.studio_termostato_2"
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "comfort"})
    hass.states.async_set(second, "cool", {"preset_mode": "comfort"})

    def policy(_state):
        return {preset_lever(CLIMATE): "economy", preset_lever(second): "economy"}

    engine = SupervisorEngine(hass, entry, coordinator, policies=[policy])
    writes = []

    async def _handler(call):
        writes.append(call.data["entity_id"])
        engine._epoch += 1  # the fail-safe hand-back lands mid-loop

    hass.services.async_register("climate", "set_preset_mode", _handler)

    await engine._run()

    # the first lever wrote; the epoch bump aborted the rest of the loop.
    assert writes == [CLIMATE]


# --- Tier-1 M1: fold wiring + fail-safe byte-gate ------------------------------

async def test_controllers_are_exactly_cooling_then_night(hass):
    """(P2 gate g) The production controllers tuple is EXACTLY
    (CoolingController, NightSilenceController) — the oracle trio cannot stay
    silently wired, and Night stays LAST (its Notte-exit one-shot manuale
    release must yield to the band re-taking a bedroom on the same cycle)."""
    from custom_components.villa_hvac.night import NightSilenceController
    from custom_components.villa_hvac.policies import CoolingController

    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    assert [type(c) for c in engine.controllers] == [
        CoolingController, NightSilenceController,
    ]
    assert engine._cooling is engine.controllers[0]


def test_failsafe_functions_byte_identical():
    """(P2 gate h) The fail-safe cluster is byte-identical to the v0.38.0
    baseline — the Tier-1 tier preserves it verbatim (STORY §6). The ONLY
    allowed changes are the two pinned hardening commits (P2 per-lever epoch
    check in _cycle, P6 boot manuale sweep in async_release_blocco) — neither
    touches these three functions. If this fails, either the change is
    unintended (revert it) or it is a NEW deliberate hardening: own commit, own
    pin, update the hash here in that same commit."""
    import hashlib
    import inspect

    from custom_components.villa_hvac.engine import SupervisorEngine

    pins = {
        "async_fail_safe":
            "bd60c3863bc5430fe925bd9df7fdd99dc374df91e1975ceeaf4bdb3378c4dfce",
        "_restore_presets":
            "f5160debf4c7d1316d2f9728555fba8b86e672a3d6590b208ae961ea46fb2c16",
        "_release_blocco":
            "0c54ed2fd9c5e233b3816101c77f4ba48d73da8ac10662a6dd0fe843740b8d22",
    }
    for name, expected in pins.items():
        src = inspect.getsource(getattr(SupervisorEngine, name))
        actual = hashlib.sha256(src.encode()).hexdigest()
        assert actual == expected, (
            f"{name} changed (sha256 {actual}) — the fail-safe is grep-gated"
        )


async def test_center_schedule_cached_not_recomputed_each_tick(hass):
    """F4c Phase 6: the reference schedule is SLOW-moving — cached across ticks
    (within the forecast cadence + same mode), not recomputed every 30 s."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )
    hass.states.async_set("sensor.clima_salotto", "24.0")
    await entry.runtime_data.async_refresh()

    await engine._tick()
    first, ts1 = engine._center_schedule_cache, engine._schedule_ts
    assert first is not None
    await engine._tick()  # within cadence + same mode -> reuse the cached object
    assert engine._center_schedule_cache is first
    assert engine._schedule_ts == ts1
