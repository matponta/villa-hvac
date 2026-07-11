"""Tests for camere silenziose: night silence + heat-guard (#2b)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    NIGHT_GUARD_HIGH,
    NIGHT_GUARD_LOW,
    DOMAIN,
)
from custom_components.villa_hvac.night import GuardState, bedrooms, evaluate_guard

from .helpers import enable_supervisor, seed_thermostats

T0 = datetime(2026, 6, 23, 2, 0, 0, tzinfo=timezone.utc)
THRESHOLD = 26.0


# --- Pure hysteresis ---------------------------------------------------------

def test_bedrooms_are_exactly_two():
    ids = {zid for zid, _ in bedrooms()}
    assert ids == {"main_bedroom", "gabriroom"}  # studio_v (Ospiti) is now office


def test_silent_stays_silent_below_threshold():
    state, action = evaluate_guard(GuardState(), 24.0, THRESHOLD, T0)
    assert action is None and not state.cooling


def test_brief_warmth_does_not_trigger_cooling():
    state = GuardState()
    state, action = evaluate_guard(state, 27.0, THRESHOLD, T0)
    assert action is None  # timer just started
    state, action = evaluate_guard(
        state, 27.0, THRESHOLD, T0 + NIGHT_GUARD_HIGH - timedelta(seconds=1)
    )
    assert action is None and not state.cooling


def test_sustained_warmth_triggers_low_cooling():
    state = GuardState(above_since=T0)
    state, action = evaluate_guard(state, 27.0, THRESHOLD, T0 + NIGHT_GUARD_HIGH)
    assert action == "cool" and state.cooling


def test_sustained_cool_silences_again():
    state = GuardState(cooling=True, below_since=T0)
    state, action = evaluate_guard(state, 24.0, THRESHOLD, T0 + NIGHT_GUARD_LOW)
    assert action == "silence" and not state.cooling


def test_cooling_holds_until_low_window_elapses():
    state = GuardState(cooling=True)
    state, action = evaluate_guard(state, 24.0, THRESHOLD, T0)
    assert action is None and state.cooling  # below timer just started


def test_missing_temp_is_noop():
    state = GuardState(cooling=True, below_since=T0)
    new, action = evaluate_guard(state, None, THRESHOLD, T0 + NIGHT_GUARD_LOW)
    assert action is None and new == state


# --- Integration: mode transitions drive the bedrooms ------------------------

async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _select_mode(hass, mode):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.house_mode", "option": mode}, blocking=True,
    )
    await hass.async_block_till_done()


@freeze_time("2026-07-04 23:30:00")
async def test_night_silences_bedrooms_and_casa_wakes(hass):
    """C1: camere silenziose flows through the arbiter — silence = manuale on
    + a fan.turn_off dispatched by the fan lever (a 0% opinion asserts the OFF
    state); waking releases manuale. Frozen to a night hour (tz pinned UTC):
    since the wake is clock-derived, a daytime run would legitimately not
    silence."""
    await hass.config.async_set_time_zone("UTC")
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass)
    # Seed the bedroom fan + manuale states so the arbiter's reconcile has a live
    # value to diff against (routing through the engine, not direct writes).
    for fan, man in (
        ("fan.fancoil_camera_padronale", "switch.fancoil_camera_padronale_manuale"),
        ("fan.fancoil_camera_gabriele", "switch.fancoil_camera_gabriele_manuale"),
    ):
        hass.states.async_set(fan, "on", {"percentage": 100})
        hass.states.async_set(man, "off")
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "climate", "set_temperature")
    sw_on = async_mock_service(hass, "switch", "turn_on")
    sw_off = async_mock_service(hass, "switch", "turn_off")
    # A 0% fan opinion dispatches fan.turn_off (asserts the verifiable OFF
    # state) PLUS set_percentage(0): these KNX fans have separate switch/speed
    # group objects and turn_off writes only the switch GA — the 0% disarms the
    # retained speed so a wall-press ON resumes silent.
    fan_off = async_mock_service(hass, "fan", "turn_off")
    fan_pct = async_mock_service(hass, "fan", "set_percentage")

    await _select_mode(hass, "Notte")

    on_targets = {c.data["entity_id"] for c in sw_on}
    assert "switch.fancoil_camera_padronale_manuale" in on_targets
    assert "switch.fancoil_camera_gabriele_manuale" in on_targets
    silenced = {c.data["entity_id"] for c in fan_off}
    assert "fan.fancoil_camera_padronale" in silenced
    assert "fan.fancoil_camera_gabriele" in silenced
    disarmed = {c.data["entity_id"] for c in fan_pct if c.data.get("percentage") == 0}
    assert disarmed == silenced  # every turn_off is paired with a 0% disarm

    # Simulate the manuale writes landing (mocked services don't update state), so
    # the wake cycle sees "on" and writes the release.
    hass.states.async_set("switch.fancoil_camera_padronale_manuale", "on")
    hass.states.async_set("switch.fancoil_camera_gabriele_manuale", "on")

    await _select_mode(hass, "Casa")

    off_targets = {c.data["entity_id"] for c in sw_off}
    assert "switch.fancoil_camera_padronale_manuale" in off_targets
    assert "switch.fancoil_camera_gabriele_manuale" in off_targets


# --- C1: NightSilenceController as a merge controller ------------------------

def _night_state(
    *, mode="Notte", night_active=True, temps=None, now=T0,
    season="summer", house_setpoint=24.0, mode_offset=3.0,
    enabled=True, paused=False, setpoint_offset=0.0,
    auto_setback=False, free_cooling=False,
):
    from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot

    temps = temps or {}
    zones = {}
    for zid, zone in bedrooms():
        zones[zid] = ZoneSnapshot(
            zone_id=zid, name=zone["name"], climate=zone.get("climate"),
            emitter="fancoil", temp=temps.get(zid), bedroom=True,
            enabled=enabled, paused=paused, setpoint_offset=setpoint_offset,
            fancoil_units=((zone["fancoils"][0], zone["manuale_switch"]),),
        )
    return HouseState(
        now=now, zones=zones, house_mode=mode, night_active=night_active,
        season=season, house_setpoint=house_setpoint, mode_offset=mode_offset,
        auto_setback=auto_setback,
        free_cool_enabled=free_cooling,
        free_cool_threshold=22.0 if free_cooling else None,
        outdoor_temp=20.0 if free_cooling else None,
    )


def _night_ctrl():
    from custom_components.villa_hvac.night import NightSilenceController
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    return NightSilenceController(None, entry, None)  # hass/coordinator unused in __call__


def test_night_controller_silences_when_active():
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    out = _night_ctrl()(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}))
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "on"
        assert out[fan_lever(zone["fancoils"][0])] == 0  # silence


def test_night_controller_heat_guard_runs_fan():
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    c = _night_ctrl()
    hot = {"main_bedroom": 28.0, "gabriroom": 28.0}  # > 26 threshold
    c(_night_state(temps=hot, now=T0))                       # starts the above-timer
    out = c(_night_state(temps=hot, now=T0 + NIGHT_GUARD_HIGH))  # sustained -> cool
    assert out[fan_lever("fan.fancoil_camera_padronale")] == NIGHT_GUARD_FAN_PCT


def test_night_controller_releases_once_on_exit():
    from custom_components.villa_hvac.supervisor import switch_lever

    c = _night_ctrl()
    c(_night_state(night_active=True))                       # manage the bedrooms
    out = c(_night_state(mode="Casa", night_active=False))   # left Notte -> release
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
    # one-shot: a second inactive cycle has no opinion (nothing left to hand back).
    assert c(_night_state(mode="Casa", night_active=False)) == {}


def test_night_controller_yields_when_inactive_and_unmanaged():
    # Never entered Notte -> no opinion at all (doesn't fight FanBand for bedrooms).
    assert _night_ctrl()(_night_state(mode="Casa", night_active=False)) == {}


# --- GOLDEN (pinned pre-v0.54.0, LIVE path): behavior that must NOT change ---

def _guard_cooling(ctrl, temps=None, **kw):
    """Advance a controller into guard-cooling; returns the cooling-cycle output."""
    hot = temps or {"main_bedroom": 26.5, "gabriroom": 26.5}
    ctrl(_night_state(temps=hot, now=T0, **kw))                # starts the above-timer
    return ctrl(_night_state(temps=hot, now=T0 + NIGHT_GUARD_HIGH, **kw))


def test_golden_silence_emits_manuale_and_fan_only():
    """Silenced bedroom (cool room): exactly {manuale on, fan 0} — the silence
    path must never grow a setpoint opinion."""
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    out = _night_ctrl()(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}))
    expected = set()
    for _zid, zone in bedrooms():
        expected |= {switch_lever(zone["manuale_switch"]), fan_lever(zone["fancoils"][0])}
    assert set(out) == expected


def test_golden_guard_fan_stage_and_manuale_unchanged():
    """Guard-active keeps the legacy fan lever exactly: manuale on + fan 33%."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    out = _guard_cooling(_night_ctrl())
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "on"
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_golden_release_without_guard_is_manuale_off_only():
    """Notte-exit hand-back for a never-guarded night: exactly the one-shot
    manuale off per bedroom (no fan, no setpoint opinions)."""
    from custom_components.villa_hvac.supervisor import switch_lever

    c = _night_ctrl()
    c(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}))
    out = c(_night_state(mode="Casa", night_active=False))
    expected = {switch_lever(zone["manuale_switch"]) for _zid, zone in bedrooms()}
    assert set(out) == expected
    assert all(v == "off" for v in out.values())
    assert c(_night_state(mode="Casa", night_active=False)) == {}


# --- v0.54.0: chilled water — guard-active also owns the setpoint ------------

def _temp_levers(out):
    return {k: v for k, v in out.items() if k.startswith("temperature:")}


def test_guard_cooling_nudges_setpoint_below_threshold():
    """Guard-active in summer: the setpoint is driven to threshold−drop so the
    EV FAN valve opens and the held 33% fan moves CHILLED air — the first-night
    26–27 dead-band fix. Fan + manuale stay exactly the legacy levers."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_SETPOINT_DROP
    from custom_components.villa_hvac.supervisor import temperature_lever

    out = _guard_cooling(_night_ctrl())  # base 24+3=27 > 25.5 -> bound inactive
    for _zid, zone in bedrooms():
        assert (
            out[temperature_lever(zone["climate"])]
            == THRESHOLD - NIGHT_GUARD_SETPOINT_DROP
        )


def test_guard_nudge_never_raises_above_mode_base():
    from custom_components.villa_hvac.supervisor import temperature_lever

    # house 22 + Notte +3 = 25 < 25.5 -> bounded at the #2a target (never raise;
    # the valve is already open at that base, so the nudge must not lift it).
    out = _guard_cooling(_night_ctrl(), house_setpoint=22.0)
    for _zid, zone in bedrooms():
        assert out[temperature_lever(zone["climate"])] == 25.0


def test_guard_nudge_bound_includes_room_trim():
    from custom_components.villa_hvac.supervisor import temperature_lever

    # per-room trim −3: base 24+3−3 = 24 -> the bound tracks what #2a would set
    out = _guard_cooling(_night_ctrl(), setpoint_offset=-3.0)
    for _zid, zone in bedrooms():
        assert out[temperature_lever(zone["climate"])] == 24.0


def test_guard_no_nudge_when_base_unknown():
    """No computable #2a base -> NO nudge (guard stays fan-only). A raw
    threshold−drop fallback could RAISE a trimmed room's setpoint (a −3 °C room
    trim puts the mode target below 25.5) — never raise, so never emit blind."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    out = _guard_cooling(_night_ctrl(), house_setpoint=None)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_guard_no_nudge_while_free_cooling():
    """Free-cooling holds the fancoils in building_protection — a setpoint under
    that BP is inert but displaced; the guard yields the lever and stays
    fan-only (the free-cool × guard escalation belongs to the outside-air
    merge design)."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    out = _guard_cooling(_night_ctrl(), free_cooling=True)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_guard_no_nudge_when_season_unknown():
    # HouseState.season defaults to None in production until detected — treat
    # exactly like winter: fan-only.
    out = _guard_cooling(_night_ctrl(), season=None)
    assert _temp_levers(out) == {}


def test_guard_no_nudge_in_winter():
    """Winter guard stays fan-only: threshold−drop sits ABOVE the winter setback
    target, and a raised setpoint on a heat-mode thermostat could heat the very
    room the guard is trying to cool."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    out = _guard_cooling(_night_ctrl(), season="winter", mode_offset=-4.0)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_guard_no_nudge_on_disabled_or_paused_zones():
    """A #10-disabled / #4-paused bedroom is building_protection-owned by the
    higher preset policies — never push a setpoint under it. The legacy fan
    stage is unchanged (the known, accepted #4 edge)."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    for kw in ({"enabled": False}, {"paused": True}):
        out = _guard_cooling(_night_ctrl(), **kw)
        assert _temp_levers(out) == {}
        for _zid, zone in bedrooms():
            assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_guard_silence_drops_the_nudge():
    """Below threshold for NIGHT_GUARD_LOW -> silence: the temperature key is
    simply not emitted, so #2a re-asserts the Notte setpoint in the same merge
    (valve closes at the mode target; chilled-water stint over)."""
    from custom_components.villa_hvac.supervisor import fan_lever

    c = _night_ctrl()
    _guard_cooling(c)                                  # nudging at 25.5
    cool = {"main_bedroom": 25.0, "gabriroom": 25.0}
    t1 = T0 + NIGHT_GUARD_HIGH + timedelta(minutes=1)
    mid = c(_night_state(temps=cool, now=t1))          # below-timer starts
    assert _temp_levers(mid) != {}                     # still cooling -> still nudged
    out = c(_night_state(temps=cool, now=t1 + NIGHT_GUARD_LOW))
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == 0


def test_release_restores_nudged_setpoint_once():
    """Auto-wake / Notte exit while the guard is nudging: the one-shot hand-back
    also writes the house-mode base back (manuale off as before). With #2a
    re-asserting from here on (auto_setback on, base computable), the snapshot
    tracking is dropped."""
    from custom_components.villa_hvac.supervisor import switch_lever, temperature_lever

    c = _night_ctrl()
    _guard_cooling(c)
    out = c(_night_state(night_active=False, auto_setback=True))  # woken, in Notte
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
        assert out[temperature_lever(zone["climate"])] == 27.0  # 24 + 3
    assert c._nudged == {}                                    # #2a owns it now
    assert c(_night_state(night_active=False, auto_setback=True)) == {}  # one-shot


def test_release_restores_from_snapshot_and_keeps_tracking_when_unprotected():
    """Release into a state where #2a will NOT re-assert (Vacanza: no offset;
    auto_setback off): the restore still goes out — from the base RECORDED at
    nudge time — but it is a single unprotected telegram, so the snapshot is
    KEPT for the fail-safe."""
    from custom_components.villa_hvac.supervisor import switch_lever, temperature_lever

    c = _night_ctrl()
    _guard_cooling(c)                                  # records base 27.0
    out = c(_night_state(mode="Vacanza", night_active=False, mode_offset=None))
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
        assert out[temperature_lever(zone["climate"])] == 27.0  # snapshot, not live
    assert set(c._nudged) == {"main_bedroom", "gabriroom"}  # fail-safe can restore


def test_guard_setpoint_outranks_house_mode_in_the_merge():
    """The engine merges controllers before the pure policies: the guard's
    temperature opinion must win the lever over house_mode #2a."""
    from dataclasses import replace as dc_replace

    from custom_components.villa_hvac.policies import house_mode_policy
    from custom_components.villa_hvac.supervisor import merge_desired, temperature_lever

    c = _night_ctrl()
    hot = {"main_bedroom": 26.5, "gabriroom": 26.5}
    c(_night_state(temps=hot, now=T0))
    state = dc_replace(
        _night_state(temps=hot, now=T0 + NIGHT_GUARD_HIGH), auto_setback=True
    )
    night_out = c(state)
    house_out = house_mode_policy(state)
    lever = temperature_lever(dict(bedrooms())["main_bedroom"]["climate"])
    assert house_out[lever] == 27.0                      # #2a wants the Notte base
    assert merge_desired([night_out, house_out])[lever] == 25.5  # the guard wins


async def test_failsafe_restores_snapshot_even_after_platform_teardown(hass):
    """async_fail_safe writes the base RECORDED AT NUDGE TIME back for
    guard-nudged bedrooms (manuale just went AUTO ~100%: a lingering
    threshold−drop setpoint would overcool a bedroom loudly all night).

    Regression for the adversarial-review MAJOR: on the unload path the
    integration's own select/number entities are torn down BEFORE
    async_fail_safe runs, so a restore computed from live reads silently
    no-ops. The snapshot must survive that teardown — simulated here by
    removing both entities before the hand-back."""
    from custom_components.villa_hvac.supervisor import LeverState, temperature_lever

    await hass.config.async_set_time_zone("UTC")
    entry = await _setup(hass)
    seed_thermostats(hass)                       # salotto 'cool' -> season summer
    coordinator = entry.runtime_data
    night = coordinator.night
    engine = coordinator.engine
    # Drive the guard into a nudge through the controller's public path
    # (records the restore snapshot: 24 + Notte +3 = 27.0 per bedroom).
    out = _guard_cooling(night)
    assert _temp_levers(out) != {}
    # Simulate the unload ordering: platform entities gone before the fail-safe.
    hass.states.async_remove("select.house_mode")
    hass.states.async_remove("number.house_setpoint")
    # A lever state committed by an actuating cycle must be dropped by the
    # hand-back so a queued cycle can't re-assert the nudge.
    lever = temperature_lever("climate.camera_padronale_termostato_2")
    engine._lever_states[lever] = LeverState(written="25.5")
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "switch", "turn_off")
    temps = async_mock_service(hass, "climate", "set_temperature")

    await engine.async_fail_safe()

    targeted = {c.data["entity_id"]: c.data["temperature"] for c in temps}
    assert targeted == {
        "climate.camera_padronale_termostato_2": 27.0,   # snapshot, not a live read
        "climate.camera_gabriele_termostato_2": 27.0,
    }
    assert night._nudged == {}
    assert lever not in engine._lever_states


async def test_engine_cycle_delivers_chilled_water(hass):
    """LIVE path (adversarial-review gap): a hot bedroom in Notte must drive
    climate.set_temperature to threshold−drop through the REAL engine cycle —
    controller opinion → controllers-before-policies merge (beating
    house_mode's 27) → reconcile diff → service call — not just as a
    controller output dict."""
    await hass.config.async_set_time_zone("UTC")
    with freeze_time("2026-07-04 23:30:00") as frozen:
        entry = await _setup(hass)
        seed_thermostats(hass, temperature=27.0)     # Notte base already applied
        hass.states.async_set("number.house_setpoint", "24.0")
        for fan, man in (
            ("fan.fancoil_camera_padronale", "switch.fancoil_camera_padronale_manuale"),
            ("fan.fancoil_camera_gabriele", "switch.fancoil_camera_gabriele_manuale"),
        ):
            hass.states.async_set(fan, "on", {"percentage": 100})
            hass.states.async_set(man, "on")         # silence already latched
        hass.states.async_set("sensor.clima_camera", "26.5")   # padronale hot
        hass.states.async_set("sensor.clima_gabri", "25.0")    # gabriele fine
        await entry.runtime_data.async_refresh()               # fuse the temps
        async_mock_service(hass, "climate", "set_preset_mode")
        async_mock_service(hass, "fan", "turn_on")
        async_mock_service(hass, "fan", "turn_off")
        async_mock_service(hass, "fan", "set_percentage")
        temps = async_mock_service(hass, "climate", "set_temperature")
        await enable_supervisor(hass)

        await _select_mode(hass, "Notte")            # pass 1: above-timer starts
        frozen.move_to("2026-07-04 23:34:00")        # > NIGHT_GUARD_HIGH later
        await entry.runtime_data.engine._run()       # pass 2: guard fires
        await hass.async_block_till_done()

    padronale = [
        c.data["temperature"] for c in temps
        if c.data["entity_id"] == "climate.camera_padronale_termostato_2"
    ]
    assert 25.5 in padronale                         # chilled water: valve opens
    gabriele = [
        c.data["temperature"] for c in temps
        if c.data["entity_id"] == "climate.camera_gabriele_termostato_2"
    ]
    assert 25.5 not in gabriele                      # cool room stays at the base


# --- Fix 1b (2026-07-04): the wake is clock-derived, not latch-only ----------

async def test_woken_derives_from_clock_after_wake_time(hass):
    """A reboot/reload in Notte AFTER the wake time loses the in-memory latch;
    `woken` must derive from the clock so the bedrooms are not re-silenced
    until the mode leaves Notte (the 3/7 morning: deploy restart at 10:09)."""
    await hass.config.async_set_time_zone("UTC")
    ctrl = _night_ctrl()
    assert ctrl._woken is False  # fresh controller == post-reboot state

    with freeze_time("2026-07-04 10:30:00"):
        assert ctrl.woken is True  # inside [08:00, 20:00) -> silence lifted
    with freeze_time("2026-07-04 03:00:00"):
        assert ctrl.woken is False  # night proper -> silence active
    with freeze_time("2026-07-04 21:30:00"):
        assert ctrl.woken is False  # early-evening Notte -> silence engages

    ctrl._woken = True  # the explicit 08:00 latch still wins at any hour
    with freeze_time("2026-07-04 03:00:00"):
        assert ctrl.woken is True


async def test_reboot_in_notte_after_wake_does_not_resilence(hass):
    """End-to-end derivation: a FRESH controller (post-restart, no latch) with
    the house still in Notte must yield night_active False during the day
    window and True at night — build_house_state reads the clock-aware woken."""
    from custom_components.villa_hvac.engine import build_house_state

    await hass.config.async_set_time_zone("UTC")
    entry = await _setup(hass)  # fresh NightSilenceController: no wake latch
    hass.states.async_set("select.house_mode", "Notte")

    with freeze_time("2026-07-04 10:30:00"):
        state = build_house_state(hass, entry, entry.runtime_data)
        assert state.house_mode == "Notte"
        assert state.night_active is False  # clock says woken -> band keeps control
    with freeze_time("2026-07-04 03:00:00"):
        state = build_house_state(hass, entry, entry.runtime_data)
        assert state.night_active is True  # night proper -> silence re-derives
