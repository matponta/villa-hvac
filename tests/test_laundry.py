"""v0.97.0 — laundry drying: short purges through the lavanderia vasistas."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.villa_hvac.supervisor.laundry import (
    CLOSE_BLIND,
    CLOSE_DISABLED,
    CLOSE_DRY,
    CLOSE_MANUAL_MAX,
    CLOSE_MAX,
    CLOSE_OUTSIDE,
    CLOSE_RAIN,
    CMD_CLOSE,
    CMD_OPEN,
    REASON_BLIND,
    REASON_IDLE,
    REASON_MANUAL,
    REASON_OFF,
    REASON_PURGE,
    REASON_REST,
    REASON_UNKNOWN,
    REASON_WAIT_COLD_ROOM,
    REASON_WAIT_OUTSIDE,
    REASON_WAIT_RAIN,
    LaundryReading,
    LaundryState,
    failed_command,
    laundry_step,
)

T0 = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
DAY = "2026-11-25"
MIN = timedelta(minutes=1)


def _r(is_open=False, *, rh=56.0, temp=20.0, out_t=7.0, out_rh=96.0,
       moving=False, raining=False):
    return LaundryReading(
        is_open=is_open, moving=moving, room_temp=temp, room_rh=rh,
        outdoor_temp=out_t, outdoor_rh=out_rh, raining=raining,
    )


def _step(reading, state=None, *, now=T0, enabled=True, day=DAY):
    return laundry_step(
        enabled=enabled, reading=reading, state=state or LaundryState(),
        now=now, local_day=day,
    )


def _purging(now=T0):
    """State + time right after we opened for a purge (cover now open)."""
    d = _step(_r(False), now=now)
    assert d.command == CMD_OPEN
    return d.state


# -- opening ------------------------------------------------------------------
def test_humid_room_with_drier_outside_starts_a_purge():
    d = _step(_r(False))
    assert d.command == CMD_OPEN and d.reason == REASON_PURGE
    assert d.state.ours and d.state.purges == 1
    assert d.ah_outdoor < d.ah_room - 1.0
    assert d.max_open == timedelta(minutes=15)   # 7 °C outside: cold caps


def test_baseline_room_does_not_open():
    d = _step(_r(False, rh=49.6, temp=19.4))
    assert d.command is None and d.reason == REASON_IDLE


def test_outside_not_drier_enough_waits():
    # 20 °C / 56 % ≈ 9.7 g/m³; 14 °C / 90 % ≈ 10.9 g/m³ — outside is wetter.
    d = _step(_r(False, out_t=14.0, out_rh=90.0))
    assert d.command is None and d.reason == REASON_WAIT_OUTSIDE


def test_rain_and_cold_room_wait():
    assert _step(_r(False, raining=True)).reason == REASON_WAIT_RAIN
    assert _step(_r(False, temp=16.5, rh=62.0)).reason == REASON_WAIT_COLD_ROOM


def test_missing_sensor_is_blind_and_never_opens():
    d = _step(LaundryReading(False, False, 20.0, None, 7.0, 96.0))
    assert d.command is None and d.reason == REASON_BLIND


def test_rest_between_purges_cold_then_mild():
    st = LaundryState(closed_since=T0)
    d = _step(_r(False), st, now=T0 + 19 * MIN)
    assert d.reason == REASON_REST and d.next_purge_at == T0 + 20 * MIN
    assert _step(_r(False), st, now=T0 + 20 * MIN).command == CMD_OPEN
    # mild: 10-min rest
    mild = _r(False, out_t=18.0, out_rh=50.0)
    assert _step(mild, st, now=T0 + 10 * MIN).command == CMD_OPEN


def test_first_look_at_boot_counts_as_rested():
    assert _step(_r(False), LaundryState()).command == CMD_OPEN


# -- closing our purge ----------------------------------------------------------
def test_purge_closes_when_dry_after_min_open():
    st = _purging()
    assert _step(_r(True, rh=49.0), st, now=T0 + 4 * MIN).command is None
    d = _step(_r(True, rh=49.0), st, now=T0 + 5 * MIN)
    assert d.command == CMD_CLOSE and d.close_reason == CLOSE_DRY


def test_purge_capped_at_15_min_when_cold_60_when_mild():
    st = _purging()
    d = _step(_r(True, rh=53.0), st, now=T0 + 15 * MIN)
    assert d.command == CMD_CLOSE and d.close_reason == CLOSE_MAX
    mild_open = _r(False, out_t=18.0, out_rh=50.0)
    st = _step(mild_open).state
    mild = _r(True, rh=53.0, out_t=18.0, out_rh=50.0)
    assert _step(mild, st, now=T0 + 30 * MIN).command is None
    assert _step(mild, st, now=T0 + 60 * MIN).close_reason == CLOSE_MAX


def test_purge_closes_on_rain_at_once_and_on_lost_sensors_after_min_open():
    st = _purging()
    assert _step(_r(True, raining=True), st, now=T0 + MIN).close_reason == CLOSE_RAIN
    blind = LaundryReading(True, False, 20.0, None, 7.0, 96.0)
    assert _step(blind, st, now=T0 + 2 * MIN).command is None
    assert _step(blind, st, now=T0 + 5 * MIN).close_reason == CLOSE_BLIND


def test_purge_closes_when_outside_stops_being_drier():
    st = _purging()
    # room cooled + still humid: 17.5 °C / 52 % ≈ 7.8 g/m³ vs outside 7.4
    d = _step(_r(True, rh=52.0, temp=17.5), st, now=T0 + 6 * MIN)
    assert d.close_reason == CLOSE_OUTSIDE


def test_close_lands_then_rest_starts():
    st = _step(_r(True, rh=49.0), _purging(), now=T0 + 5 * MIN).state
    d = _step(_r(False, rh=58.0), st, now=T0 + 6 * MIN)
    assert d.command is None and d.reason == REASON_REST
    assert not d.state.ours and d.state.closed_since == T0 + 6 * MIN


def test_command_in_flight_is_waited_not_misread():
    st = _purging()
    # still reports closed 30 s after our open: not a "closed edge"
    d = _step(_r(False), st, now=T0 + timedelta(seconds=30))
    assert d.command is None and d.state.ours and d.reason == REASON_PURGE
    # close in flight: still reports open — no duplicate command
    st = _step(_r(True), st, now=T0 + MIN).state
    st = _step(_r(True, rh=49.0), st, now=T0 + 6 * MIN).state
    assert _step(_r(True, rh=49.0), st, now=T0 + 6 * MIN + timedelta(seconds=30)).command is None
    # a close that never landed is re-commanded after the grace
    again = _step(_r(True, rh=49.0), st, now=T0 + 8 * MIN)
    assert again.command == CMD_CLOSE


def test_moving_or_unknown_holds():
    st = _purging()
    assert _step(_r(True, moving=True), st, now=T0 + 20 * MIN).reason == REASON_UNKNOWN
    assert _step(_r(None), st, now=T0 + 20 * MIN).command is None


# -- manual openings ------------------------------------------------------------
def test_manual_opening_respected_then_closed_after_60_min_when_cold():
    st = _step(_r(False, rh=48.0)).state
    d = _step(_r(True, rh=48.0), st, now=T0 + MIN)
    assert d.reason == REASON_MANUAL and not d.state.ours
    st = d.state
    assert _step(_r(True), st, now=T0 + 60 * MIN).command is None
    d = _step(_r(True), st, now=T0 + 61 * MIN)
    assert d.command == CMD_CLOSE and d.close_reason == CLOSE_MANUAL_MAX


def test_manual_opening_in_mild_weather_gets_8_hours():
    mild = dict(out_t=20.0, out_rh=50.0)
    st = _step(_r(True, **mild)).state
    assert _step(_r(True, **mild), st, now=T0 + 7 * 60 * MIN).command is None
    assert _step(_r(True, **mild), st, now=T0 + 8 * 60 * MIN).command == CMD_CLOSE


def test_hand_close_during_our_purge_ends_it():
    st = _step(_r(True), _purging(), now=T0 + MIN).state
    d = _step(_r(False), st, now=T0 + 3 * MIN)
    assert not d.state.ours and d.reason == REASON_REST


def test_hand_reopen_after_our_close_is_manual():
    st = _step(_r(True, rh=49.0), _purging(), now=T0 + 5 * MIN).state
    st = _step(_r(False), st, now=T0 + 6 * MIN).state
    d = _step(_r(True), st, now=T0 + 7 * MIN)
    assert d.reason == REASON_MANUAL and not d.state.ours


# -- feature off ------------------------------------------------------------------
def test_disabled_closes_our_purge_but_never_a_manual_opening():
    st = _step(_r(True), _purging(), now=T0 + MIN).state
    d = _step(_r(True), st, now=T0 + 2 * MIN, enabled=False)
    assert d.command == CMD_CLOSE and d.close_reason == CLOSE_DISABLED
    manual = _step(_r(True), enabled=False)
    assert manual.command is None and manual.reason == REASON_OFF
    later = _step(_r(True), manual.state, now=T0 + 10 * 60 * MIN, enabled=False)
    assert later.command is None


def test_disabled_never_opens():
    assert _step(_r(False), enabled=False).command is None


# -- bookkeeping --------------------------------------------------------------------
def test_failed_open_is_undone():
    before = LaundryState(closed_since=T0 - 30 * MIN)
    d = _step(_r(False), before)
    undone = failed_command(before, d.state, CMD_OPEN)
    assert not undone.ours and undone.open_since is None and undone.purges == 0
    assert undone.closed_since == before.closed_since


def test_purge_counter_resets_each_day():
    st = _purging()
    assert st.purges == 1
    d = _step(_r(False, rh=40.0), st, now=T0 + 3 * 60 * MIN, day="2026-11-26")
    assert d.state.purges == 0


def test_replay_2026_11_25_night_never_stays_open():
    """Laundry keeps raising the room after each close (as on 25/11): the law
    cycles short purges and the vasistas is never open more than 15 min."""
    st = LaundryState()
    now = T0
    open_for = timedelta(0)
    longest = timedelta(0)
    rh = 48.0
    is_open = False
    for _ in range(8 * 60 * 2):   # 8 h of 30-s ticks
        d = _step(_r(is_open, rh=rh, temp=20.0), st, now=now)
        st = d.state
        if d.command == CMD_OPEN:
            is_open = True
        elif d.command == CMD_CLOSE:
            is_open = False
        open_for = open_for + timedelta(seconds=30) if is_open else timedelta(0)
        longest = max(longest, open_for)
        # wet laundry: +0.5 %/min closed, the purge drops it 1.5 %/min
        rh = max(48.0, rh - 0.75) if is_open else min(64.0, rh + 0.25)
        now += timedelta(seconds=30)
    assert longest <= timedelta(minutes=15)
    assert st.purges >= 8


# -- HA wiring ------------------------------------------------------------------------
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.villa_hvac.const import (  # noqa: E402
    DOMAIN,
    LAUNDRY_VASISTAS,
    SEASON_REFERENCE_CLIMATE,
    WINDOW_OPEN_DELAY,
)
from custom_components.villa_hvac.laundry import (  # noqa: E402
    LaundryController,
    _decode,
    _encode,
)

from .helpers import enable_supervisor, seed_thermostats  # noqa: E402

LAUNDRY_CLIMATE = "climate.lavanderia_termostato_2"


async def _setup(hass, *, master=True):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if master:
        await enable_supervisor(hass)
    return entry


def _air(hass, *, rh="58", vasistas="closed"):
    hass.states.async_set("sensor.umidita_lavanderia", rh)
    hass.states.async_set("sensor.clima_lavanderia", "20.0")
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "7.0")
    hass.states.async_set("sensor.gw3000a_humidity", "96")
    hass.states.async_set("sensor.gw3000a_rain_rate", "0.0")
    hass.states.async_set(LAUNDRY_VASISTAS, vasistas)


async def test_controller_opens_a_purge_only_with_the_master_on(hass):
    entry = await _setup(hass, master=False)
    opens = async_mock_service(hass, "cover", "open_cover")
    _air(hass)
    ctrl: LaundryController = entry.runtime_data.laundry
    await ctrl.async_evaluate()
    assert opens == [] and ctrl.view["reason"] == "spento"
    await enable_supervisor(hass)
    await ctrl.async_evaluate()
    assert [c.data["entity_id"] for c in opens] == [LAUNDRY_VASISTAS]
    assert ctrl.state.ours and ctrl.view["reason"] == "ricambio"
    assert ctrl.view["ah_outdoor"] < ctrl.view["ah_room"]
    state = hass.states.get("sensor.lavanderia_asciugatura")
    assert state is not None


async def test_our_purge_keeps_the_heating_on_a_manual_opening_pauses(hass):
    entry = await _setup(hass)
    seed_thermostats(hass, hvac="heat", preset="comfort")
    async_mock_service(hass, "cover", "open_cover")
    presets = async_mock_service(hass, "climate", "set_preset_mode")
    _air(hass)
    ctrl: LaundryController = entry.runtime_data.laundry
    await ctrl.async_evaluate()                       # our purge starts
    hass.states.async_set(LAUNDRY_VASISTAS, "open")   # ...and lands
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert "lavanderia" not in entry.runtime_data.window.paused
    assert not [c for c in presets if c.data["entity_id"] == LAUNDRY_CLIMATE]

    # a hand opening (not ours) pauses as any open window
    hass.states.async_set(LAUNDRY_VASISTAS, "closed")
    await hass.async_block_till_done()
    ctrl.state = LaundryState(closed_since=dt_util.utcnow())
    hass.states.async_set(LAUNDRY_VASISTAS, "open")
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + 2 * WINDOW_OPEN_DELAY + timedelta(seconds=10)
    )
    await hass.async_block_till_done()
    assert "lavanderia" in entry.runtime_data.window.paused


async def test_switch_off_and_release_close_our_purge_only(hass):
    entry = await _setup(hass)
    closes = async_mock_service(hass, "cover", "close_cover")
    _air(hass, vasistas="open")
    ctrl: LaundryController = entry.runtime_data.laundry
    now = dt_util.utcnow()
    ctrl.state = LaundryState(ours=True, open_since=now - 2 * MIN)
    await ctrl.async_release()
    assert len(closes) == 1 and not ctrl.state.ours

    # manual opening: release never touches it
    ctrl.state = LaundryState(open_since=now - 2 * MIN)
    await ctrl.async_release()
    assert len(closes) == 1

    # switch OFF closes a purge of ours at once
    ctrl.state = LaundryState(ours=True, open_since=now - 2 * MIN)
    reg = er.async_get(hass)
    sw = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_laundry_drying")
    assert hass.states.get(sw).state == "on"          # default ON
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
    assert len(closes) == 2


async def test_manual_opening_closed_after_an_hour_in_the_cold_with_a_page(hass):
    entry = await _setup(hass)
    closes = async_mock_service(hass, "cover", "close_cover")
    pages = async_mock_service(hass, "notify", "mobile_app_matphone16")
    async_mock_service(hass, "notify", "mobile_app_pixel_10")
    _air(hass, rh="48", vasistas="open")
    ctrl: LaundryController = entry.runtime_data.laundry
    ctrl.state = LaundryState(open_since=dt_util.utcnow() - 61 * MIN)
    await ctrl.async_evaluate()
    assert len(closes) == 1
    assert ctrl.view["last_close_reason"] == CLOSE_MANUAL_MAX
    assert len(pages) == 1 and "lavanderia" in pages[0].data["message"]


async def test_failed_open_is_retried_next_cycle(hass, monkeypatch):
    entry = await _setup(hass)
    _air(hass)
    ctrl: LaundryController = entry.runtime_data.laundry

    async def _fail(self, command):
        return False

    monkeypatch.setattr(LaundryController, "_write", _fail)
    await ctrl.async_evaluate()
    assert not ctrl.state.ours and ctrl.state.purges == 0


def test_store_roundtrip():
    st = LaundryState(
        ours=True, open_since=T0, closed_since=None, commanded_at=T0,
        commanded=CMD_OPEN, purges_day=DAY, purges=3,
    )
    assert _decode(_encode(st)) == st
    assert _decode(None) == LaundryState()
    assert _decode({"commanded": "bogus", "purges": "x"}) == LaundryState()
