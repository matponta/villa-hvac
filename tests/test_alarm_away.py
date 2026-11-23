"""v0.95.0 — Via from the burglar alarm (STORY_VIA_ANTIFURTO.md)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from homeassistant.util import dt as dt_util

from custom_components.villa_hvac.away import (
    ALARM_REASON_MODE,
    ALARM_REASON_NOT_ARMED,
    ALARM_REASON_PRESENCE_HOME,
    ALARM_REASON_PRESENCE_UNKNOWN,
    ALARM_REASON_READY,
    ALARM_REASON_SETBACK_OFF,
    ALARM_REASON_SWITCH_OFF,
    ALARM_REASON_WAITING,
    alarm_away_due,
    alarm_fire_at,
)
from custom_components.villa_hvac.const import (
    ALARM_MIRROR,
    DOMAIN,
    PRESENCE_PERSONS,
    SEASON_REFERENCE_CLIMATE,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.villa_hvac.returnhome import quiet_until

SELECT = "select.house_mode"
SWITCH = "switch.via_da_antifurto"
SENSOR = "sensor.via_da_antifurto"
NUM_SUMMER = "number.via_da_antifurto_ritardo_estate"
NUM_WINTER = "number.via_da_antifurto_ritardo_inverno"
PRECOND = "switch.return_pre_cond"

T0 = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
DELAYS = {SEASON_SUMMER: timedelta(minutes=30), SEASON_WINTER: timedelta(hours=8)}


def _due(now, *, mirror="Inserito", season=SEASON_WINTER, presence="not_home",
         mode="Casa", enabled=True, auto_setback=True, armed_since=T0):
    return alarm_away_due(mirror, armed_since, now, season, DELAYS, presence, mode,
                          enabled, auto_setback)


# --- Pure --------------------------------------------------------------------

def test_winter_waits_the_full_eight_hours():
    assert _due(T0 + timedelta(hours=7, minutes=59)) == (False, ALARM_REASON_WAITING)
    assert _due(T0 + timedelta(hours=8)) == (True, ALARM_REASON_READY)


def test_summer_fires_after_thirty_minutes():
    assert _due(T0 + timedelta(minutes=29), season=SEASON_SUMMER)[0] is False
    assert _due(T0 + timedelta(minutes=30), season=SEASON_SUMMER) == (
        True, ALARM_REASON_READY)


@pytest.mark.parametrize("mirror", ["Inserito notte", "Disinserito", "Sconosciuto",
                                    "Allarme", None, "unavailable"])
def test_only_inserito_counts(mirror):
    assert _due(T0 + timedelta(hours=9), mirror=mirror) == (
        False, ALARM_REASON_NOT_ARMED)


def test_presence_home_or_unknown_blocks():
    late = T0 + timedelta(hours=9)
    assert _due(late, presence="home") == (False, ALARM_REASON_PRESENCE_HOME)
    assert _due(late, presence=None) == (False, ALARM_REASON_PRESENCE_UNKNOWN)


@pytest.mark.parametrize("mode", ["Via", "Vacanza"])
def test_away_modes_are_left_alone(mode):
    assert _due(T0 + timedelta(hours=9), mode=mode) == (
        False, f"{ALARM_REASON_MODE}{mode.lower()}")


def test_notte_escalates_like_casa():
    assert _due(T0 + timedelta(hours=9), mode="Notte")[0] is True


def test_switches_off_block():
    late = T0 + timedelta(hours=9)
    assert _due(late, enabled=False) == (False, ALARM_REASON_SWITCH_OFF)
    assert _due(late, auto_setback=False) == (False, ALARM_REASON_SETBACK_OFF)


def test_season_change_mid_episode_moves_fire_at():
    assert alarm_fire_at(T0, SEASON_WINTER, DELAYS) == T0 + timedelta(hours=8)
    assert alarm_fire_at(T0, SEASON_SUMMER, DELAYS) == T0 + timedelta(minutes=30)
    assert alarm_fire_at(None, SEASON_SUMMER, DELAYS) is None
    # 2 h into a winter episode the season flips to summer: due at once.
    assert _due(T0 + timedelta(hours=2), season=SEASON_SUMMER)[0] is True


def test_quiet_window_wraps_midnight():
    tz = timezone.utc
    start, end = time(22, 0), time(7, 30)
    assert quiet_until(datetime(2026, 11, 24, 3, 0, tzinfo=tz), start, end) == (
        datetime(2026, 11, 24, 7, 30, tzinfo=tz))
    assert quiet_until(datetime(2026, 11, 23, 23, 0, tzinfo=tz), start, end) == (
        datetime(2026, 11, 24, 7, 30, tzinfo=tz))
    assert quiet_until(datetime(2026, 11, 24, 15, 0, tzinfo=tz), start, end) is None
    assert quiet_until(datetime(2026, 11, 24, 7, 30, tzinfo=tz), start, end) is None
    assert quiet_until(datetime(2026, 11, 24, 3, 0, tzinfo=tz), end, end) is None


# --- With HA -----------------------------------------------------------------

def _local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute,
                    tzinfo=dt_util.get_default_time_zone())


def _presence(hass, state: str) -> None:
    for person in PRESENCE_PERSONS:
        hass.states.async_set(person, state)


async def _setup(hass, *, season_mode="heat", switch_on=True):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, season_mode)
    hass.states.async_set(ALARM_MIRROR, "Disinserito")
    _presence(hass, "home")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for domain, service in (("climate", "set_preset_mode"),
                            ("climate", "set_temperature"),
                            ("climate", "set_hvac_mode")):
        async_mock_service(hass, domain, service)
    if switch_on:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        await hass.async_block_till_done()
    return entry


async def _leave(hass) -> None:
    """Everyone out: trackers away, then the alarm armed."""
    _presence(hass, "not_home")
    hass.states.async_set(ALARM_MIRROR, "Inserito")
    await hass.async_block_till_done()


async def _advance(hass, freezer, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _select(hass, mode: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": mode},
        blocking=True)
    await hass.async_block_till_done()


async def test_entities_exist_and_switch_is_born_off(hass):
    await _setup(hass, switch_on=False)
    assert hass.states.get(SWITCH).state == "off"
    assert float(hass.states.get(NUM_SUMMER).state) == 30
    assert float(hass.states.get(NUM_WINTER).state) == 8
    assert hass.states.get(SENSOR).state == "spento"


async def test_summer_fires_once_and_respects_a_manual_casa(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass, season_mode="cool")
    await _leave(hass)
    st = hass.states.get(SENSOR)
    assert st.state == "in_attesa"
    assert st.attributes["season"] == SEASON_SUMMER
    fire_at = dt_util.parse_datetime(st.attributes["fire_at"])
    armed = dt_util.parse_datetime(st.attributes["armed_since"])
    assert fire_at - armed == timedelta(minutes=30)

    await _advance(hass, freezer, timedelta(minutes=29))
    assert hass.states.get(SELECT).state == "Casa"
    await _advance(hass, freezer, timedelta(minutes=2))
    assert hass.states.get(SELECT).state == "Via"
    assert hass.states.get(SENSOR).state == "scattato"

    # Casa by hand (pre-heating for the return): never Via again this episode.
    await _select(hass, "Casa")
    await _advance(hass, freezer, timedelta(hours=2))
    assert hass.states.get(SELECT).state == "Casa"
    assert hass.states.get(SENSOR).state == "scattato"


async def test_manual_mode_change_before_the_fire_cancels(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass, season_mode="cool")
    await _leave(hass)
    await _advance(hass, freezer, timedelta(minutes=10))
    await _select(hass, "Notte")
    assert hass.states.get(SENSOR).state == "annullato"
    await _advance(hass, freezer, timedelta(hours=1))
    assert hass.states.get(SELECT).state == "Notte"


async def test_restart_mid_episode_keeps_fire_at(hass, freezer):
    freezer.move_to(_local(23, 9))
    entry = await _setup(hass)          # winter: 8 h
    await _leave(hass)
    fire_at = hass.states.get(SENSOR).attributes["fire_at"]
    await _advance(hass, freezer, timedelta(hours=3))

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    st = hass.states.get(SENSOR)
    assert st.state == "in_attesa"
    assert st.attributes["fire_at"] == fire_at

    await _advance(hass, freezer, timedelta(hours=4, minutes=59))
    assert hass.states.get(SELECT).state == "Casa"
    await _advance(hass, freezer, timedelta(minutes=2))
    assert hass.states.get(SELECT).state == "Via"


async def test_winter_short_outing_does_not_touch_the_mode(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass)
    await _leave(hass)
    await _advance(hass, freezer, timedelta(hours=7))
    hass.states.async_set(ALARM_MIRROR, "Disinserito")
    _presence(hass, "home")
    await hass.async_block_till_done()
    await _advance(hass, freezer, timedelta(hours=2))
    assert hass.states.get(SELECT).state == "Casa"
    assert hass.states.get(SENSOR).state == "inattivo"


async def test_disarm_keeps_via_and_presence_restores_casa(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass, season_mode="cool")
    await _leave(hass)
    await _advance(hass, freezer, timedelta(minutes=31))
    assert hass.states.get(SELECT).state == "Via"

    # Scheduled Ajax disarm for the cleaner: the house stays in Via.
    hass.states.async_set(ALARM_MIRROR, "Disinserito")
    await hass.async_block_till_done()
    await _advance(hass, freezer, timedelta(minutes=10))
    assert hass.states.get(SELECT).state == "Via"

    # An adult back home: #2c restores Casa (existing behaviour).
    _presence(hass, "home")
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "Casa"


async def test_presence_home_blocks_a_stale_mirror(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass, season_mode="cool")
    hass.states.async_set(ALARM_MIRROR, "Inserito")   # trackers still home
    await hass.async_block_till_done()
    await _advance(hass, freezer, timedelta(hours=1))
    assert hass.states.get(SELECT).state == "Casa"
    assert hass.states.get(SENSOR).attributes["reason"] == ALARM_REASON_PRESENCE_HOME


async def _notify_setup(hass, freezer, start: datetime):
    freezer.move_to(start)
    await _setup(hass, season_mode="cool")
    calls = async_mock_service(hass, "notify", "mobile_app_test")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": PRECOND}, blocking=True)
    await hass.async_block_till_done()
    return calls


async def test_night_fire_defers_the_return_ask_to_0730(hass, freezer):
    calls = await _notify_setup(hass, freezer, _local(24, 2, 30))
    await _leave(hass)
    await _advance(hass, freezer, timedelta(minutes=31))     # fires ~03:01
    assert hass.states.get(SELECT).state == "Via"
    assert calls == []
    freezer.move_to(_local(24, 7, 29))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls == []
    freezer.move_to(_local(24, 7, 30, ))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_day_fire_asks_at_once(hass, freezer):
    calls = await _notify_setup(hass, freezer, _local(24, 14, 30))
    await _leave(hass)
    await _advance(hass, freezer, timedelta(minutes=31))     # fires ~15:01
    assert hass.states.get(SELECT).state == "Via"
    assert len(calls) == 1


async def test_tracker_escalation_still_works_with_the_switch_off(hass, freezer):
    freezer.move_to(_local(23, 9))
    await _setup(hass, switch_on=False)
    await _leave(hass)
    await _advance(hass, freezer, timedelta(hours=9))
    assert hass.states.get(SELECT).state == "Casa"        # alarm path is off
    assert hass.states.get(SENSOR).state == "spento"
    await _advance(hass, freezer, timedelta(hours=9, minutes=1))
    assert hass.states.get(SELECT).state == "Via"         # 18 h safety net
