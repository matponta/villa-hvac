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


async def test_night_silence_selection_switches_default_on(hass):
    await _setup(hass)
    assert hass.states.get("switch.main_bedroom_night_silence").state == "on"
    assert hass.states.get("switch.gabriroom_night_silence").state == "on"


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
    fan_on = async_mock_service(hass, "fan", "turn_on")

    await _select_mode(hass, "Notte")

    on_targets = {c.data["entity_id"] for c in sw_on}
    assert "switch.fancoil_camera_padronale_manuale" in on_targets
    assert "switch.fancoil_camera_gabriele_manuale" in on_targets
    silenced = {c.data["entity_id"] for c in fan_off}
    assert "fan.fancoil_camera_padronale" in silenced
    assert "fan.fancoil_camera_gabriele" in silenced
    disarmed = {c.data["entity_id"] for c in fan_pct if c.data.get("percentage") == 0}
    assert disarmed == silenced  # every turn_off is paired with a 0% disarm

    # Simulate the silence writes landing (mocked services don't update state):
    # manuale ON + the fan switched OFF — the exact dead-fan state at wake.
    hass.states.async_set("switch.fancoil_camera_padronale_manuale", "on")
    hass.states.async_set("switch.fancoil_camera_gabriele_manuale", "on")
    hass.states.async_set("fan.fancoil_camera_padronale", "off", {"percentage": 0})
    hass.states.async_set("fan.fancoil_camera_gabriele", "off", {"percentage": 0})

    await _select_mode(hass, "Casa")

    off_targets = {c.data["entity_id"] for c in sw_off}
    assert "switch.fancoil_camera_padronale_manuale" in off_targets
    assert "switch.fancoil_camera_gabriele_manuale" in off_targets
    # v0.56.0 dead-fan-at-wake: waking a mild-night silence (fan left OFF) also
    # re-arms the fan, so KNX AUTO gets a LIVE fan to drive, not a dead switch.
    rearmed = {c.data["entity_id"] for c in fan_on}
    assert rearmed == {
        "fan.fancoil_camera_padronale", "fan.fancoil_camera_gabriele",
    }


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


def test_night_controller_honors_all_room_selections(hass):
    from custom_components.villa_hvac.night import NightSilenceController
    from custom_components.villa_hvac.supervisor import switch_lever

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    state = _night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0})
    expected_switches = {
        zid: switch_lever(zone["manuale_switch"]) for zid, zone in bedrooms()
    }
    for padronale, gabriele in ((False, False), (True, False), (False, True), (True, True)):
        hass.states.async_set(
            "switch.main_bedroom_night_silence", "on" if padronale else "off"
        )
        hass.states.async_set(
            "switch.gabriroom_night_silence", "on" if gabriele else "off"
        )
        out = NightSilenceController(hass, entry, None)(state)
        assert (expected_switches["main_bedroom"] in out) is padronale
        assert (expected_switches["gabriroom"] in out) is gabriele


def test_turning_selection_off_mid_night_releases_only_that_room(hass):
    from custom_components.villa_hvac.night import NightSilenceController
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    c = NightSilenceController(hass, entry, None)
    hass.states.async_set("switch.main_bedroom_night_silence", "on")
    hass.states.async_set("switch.gabriroom_night_silence", "on")
    state = _night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0})
    c(state)
    hass.states.async_set("switch.main_bedroom_night_silence", "off")
    out = c(state)
    zones = dict(bedrooms())
    assert out[switch_lever(zones["main_bedroom"]["manuale_switch"])] == "off"
    assert out[fan_lever(zones["main_bedroom"]["fancoils"][0])] == 33
    assert out[switch_lever(zones["gabriroom"]["manuale_switch"])] == "on"


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


def test_release_without_guard_rearms_fan():
    """v0.56.0 dead-fan-at-wake: a never-guarded night silenced the fan (its KNX
    ON/OFF object written OFF); KNX AUTO will NOT restart it, so the Notte-exit
    hand-back ALSO re-arms it with a one-shot fan turn-on (NIGHT_GUARD_FAN_PCT;
    AUTO re-drives the % once alive). Exactly {manuale off, fan 33} per bedroom —
    no setpoint opinion (never nudged). (Was test_golden_release_..._manuale_off_
    only pre-v0.56.0: the deliberate behavior change that fixes the dead fan.)"""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    c = _night_ctrl()
    c(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}))  # silence, no guard
    out = c(_night_state(mode="Casa", night_active=False))
    expected = set()
    for _zid, zone in bedrooms():
        expected |= {switch_lever(zone["manuale_switch"]), fan_lever(zone["fancoils"][0])}
    assert set(out) == expected
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT
    assert c(_night_state(mode="Casa", night_active=False)) == {}  # one-shot


def test_guard_fired_release_does_not_rearm_fan():
    """GOLDEN (v0.56.0): a guard actively cooling at hand-back already has a LIVE
    fan (33%), so the release must NOT emit a fan lever — the guard-fired
    Notte-exit stays byte-identical to pre-v0.56.0 (manuale off + the v0.54.0
    setpoint restore). Only a fan the silence left OFF is re-armed."""
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    c = _night_ctrl()
    _guard_cooling(c)                                    # guard cooling -> fan alive
    out = c(_night_state(mode="Casa", night_active=False))
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
        assert fan_lever(zone["fancoils"][0]) not in out  # already live -> no re-arm


def test_release_paused_bedroom_skips_fan_rearm():
    """A bedroom still #4-paused at hand-back stays quiet: building_protection
    holds the zone, and a fan would only stir warm air into the open window. The
    manuale is released; NO fan turn-on is emitted (the engine self-heal watchdog
    re-arms it once the window closes and the pause clears)."""
    from custom_components.villa_hvac.supervisor import fan_lever, switch_lever

    c = _night_ctrl()
    c(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}, paused=True))
    out = c(_night_state(mode="Casa", night_active=False, paused=True))
    for _zid, zone in bedrooms():
        assert out[switch_lever(zone["manuale_switch"])] == "off"
        assert fan_lever(zone["fancoils"][0]) not in out


def test_release_free_cooling_skips_fan_rearm():
    """While free-cooling coasts (BP, valve shut) the hand-back skips the fan
    re-arm too — the watchdog re-arms once the coast ends."""
    from custom_components.villa_hvac.supervisor import fan_lever

    c = _night_ctrl()
    c(_night_state(temps={"main_bedroom": 24.0, "gabriroom": 24.0}, free_cooling=True))
    out = c(_night_state(mode="Casa", night_active=False, free_cooling=True))
    for _zid, zone in bedrooms():
        assert fan_lever(zone["fancoils"][0]) not in out


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


def test_guard_fully_silent_while_free_cooling():
    """Free-cooling holds the fancoils in building_protection — a setpoint under
    that BP is inert but displaced, and the 33% stage could only stir warm room
    air against a shut valve. The guard yields BOTH levers (v0.55.0: the fan
    matches the v0.54.0 nudge yield; escalation belongs to the outside-air
    merge design)."""
    from custom_components.villa_hvac.supervisor import fan_lever

    out = _guard_cooling(_night_ctrl(), free_cooling=True)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == 0


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


def test_guard_no_nudge_on_disabled_zone_fan_unchanged():
    """A #10-disabled bedroom is building_protection-owned by the disable
    policy — never push a setpoint under it. The legacy fan stage stays."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    out = _guard_cooling(_night_ctrl(), enabled=False)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == NIGHT_GUARD_FAN_PCT


def test_guard_fully_silent_on_paused_zone():
    """v0.55.0 (window contacts): a #4-paused bedroom gets NO nudge AND no fan —
    the guard used to blow warm air into the open window (the old accepted #4
    edge, closed now that the bedrooms have real contacts). Closing the window
    resumes the stage: the hysteresis kept advancing."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT
    from custom_components.villa_hvac.supervisor import fan_lever

    c = _night_ctrl()
    out = _guard_cooling(c, paused=True)
    assert _temp_levers(out) == {}
    for _zid, zone in bedrooms():
        assert out[fan_lever(zone["fancoils"][0])] == 0
    # window closes -> paused clears -> stage (and nudge) resume immediately
    hot = {"main_bedroom": 26.5, "gabriroom": 26.5}
    out = c(_night_state(temps=hot, now=T0 + NIGHT_GUARD_HIGH + timedelta(minutes=1)))
    assert _temp_levers(out) != {}
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


async def test_failsafe_revives_supervisor_silenced_fan(hass):
    """Dead-fan-at-wake (v0.56.0): async_fail_safe re-arms any fan the supervisor
    itself switched off (a #2b silence), so 'fans -> AUTO' hands back a LIVE fan.
    A KNX fancoil in AUTO will NOT restart a fan whose switch object was left off,
    so releasing manuale alone would strand the zone (valve interlocked shut)."""
    from custom_components.villa_hvac.const import NIGHT_GUARD_FAN_PCT

    await hass.config.async_set_time_zone("UTC")
    with freeze_time("2026-07-04 23:30:00"):
        entry = await _setup(hass)
        await enable_supervisor(hass)
        seed_thermostats(hass)                       # salotto 'cool' -> summer
        for fan, man in (
            ("fan.fancoil_camera_padronale", "switch.fancoil_camera_padronale_manuale"),
            ("fan.fancoil_camera_gabriele", "switch.fancoil_camera_gabriele_manuale"),
        ):
            hass.states.async_set(fan, "on", {"percentage": 100})
            hass.states.async_set(man, "off")
        async_mock_service(hass, "climate", "set_preset_mode")
        async_mock_service(hass, "climate", "set_temperature")
        async_mock_service(hass, "switch", "turn_on")
        async_mock_service(hass, "switch", "turn_off")
        async_mock_service(hass, "fan", "turn_off")
        async_mock_service(hass, "fan", "set_percentage")
        fan_on = async_mock_service(hass, "fan", "turn_on")

        await _select_mode(hass, "Notte")            # silence -> fan.turn_off dispatched
        engine = entry.runtime_data.engine
        assert engine._fans_turned_off == {
            "fan.fancoil_camera_padronale", "fan.fancoil_camera_gabriele",
        }

        await engine.async_fail_safe()

    rearmed = {
        c.data["entity_id"] for c in fan_on
        if c.data.get("percentage") == NIGHT_GUARD_FAN_PCT
    }
    assert rearmed == {
        "fan.fancoil_camera_padronale", "fan.fancoil_camera_gabriele",
    }
    assert engine._fans_turned_off == set()          # cleared after the hand-back


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
