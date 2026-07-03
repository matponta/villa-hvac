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

def _night_state(*, mode="Notte", night_active=True, temps=None, now=T0):
    from custom_components.villa_hvac.supervisor import HouseState, ZoneSnapshot

    temps = temps or {}
    zones = {}
    for zid, zone in bedrooms():
        zones[zid] = ZoneSnapshot(
            zone_id=zid, name=zone["name"], climate=zone.get("climate"),
            emitter="fancoil", temp=temps.get(zid), bedroom=True,
            fancoil_units=((zone["fancoils"][0], zone["manuale_switch"]),),
        )
    return HouseState(now=now, zones=zones, house_mode=mode, night_active=night_active)


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
