"""Tests for window pause (#4)."""
from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from homeassistant.util import dt as dt_util

from custom_components.villa_hvac.const import DOMAIN, WINDOW_OPEN_DELAY
from custom_components.villa_hvac.window import window_zones

from .helpers import enable_supervisor, seed_thermostats

WINDOW = "cover.vasistas_gabriele"  # this vasistas is in the Gabriele BATHROOM
CLIMATE = "climate.bagno_gabriele_termostato_2"


def test_window_zones_maps_vasistas_and_contacts():
    """3 legacy bathroom vasistas (covers) + the 6 Shelly BLU contacts fitted
    2026-07-11. Porta Cucina maps to the living_room LEADER (the kitchen has no
    thermostat — open space, one air volume with the Salotto)."""
    mapping = dict(window_zones())
    assert mapping == {
        "bagno_gabriele": "cover.vasistas_gabriele",
        "bagno_giochi": "cover.vasistas_bagno_sala_giochi",
        "lavanderia": "cover.vasistas_lavanderia",
        "main_bedroom": "binary_sensor.main_bedroom_finestra_piccola_bedroom_window",
        "gabriroom": "binary_sensor.gabri_room_finestra_g_window",
        "studio_v": "binary_sensor.aaa_window",
        "office": "binary_sensor.shelly_blu_door_window_9756_window",
        "ingresso": "binary_sensor.entrance_porta_vetri_ingresso_window",
        "living_room": "binary_sensor.shelly_blu_door_window_b50c_window",
    }


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_open_window_pauses_after_debounce(hass):
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    # Nothing yet — still inside the debounce window.
    assert calls == []

    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    # Window pause (priority) -> BP on the windowed zone only. (A coordinator
    # tick may re-assert it since the mock never applies the state — that's the
    # dropped-telegram path — so assert what's written, not an exact count.)
    assert calls
    assert all(c.data["entity_id"] == CLIMATE for c in calls)
    assert all(c.data["preset_mode"] == "building_protection" for c in calls)


async def test_brief_open_then_close_does_not_pause(hass):
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    hass.states.async_set(WINDOW, "closed")  # closed before debounce elapses
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert calls == []  # timer was cancelled -> never paused -> no write


async def test_contact_pauses_bedroom_and_close_restores(hass):
    """v0.55.0: a Shelly BLU contact (binary_sensor, on=open) drives the same #4
    pause/restore as the legacy vasistas covers."""
    contact = "binary_sensor.main_bedroom_finestra_piccola_bedroom_window"
    climate = "climate.camera_padronale_termostato_2"
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(contact, "on")            # window opens
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert calls
    assert all(c.data["entity_id"] == climate for c in calls)
    assert all(c.data["preset_mode"] == "building_protection" for c in calls)

    calls.clear()
    hass.states.async_set(climate, "cool", {"preset_mode": "building_protection"})
    hass.states.async_set(contact, "off")           # window closes
    await hass.async_block_till_done()

    assert calls
    assert all(c.data["preset_mode"] == "comfort" for c in calls)  # Casa restore


async def test_kitchen_door_pauses_the_living_room_leader(hass):
    """Porta Cucina open -> building_protection on the SALOTTO thermostat (the
    kitchen has none; the leader drives both valves of the open space)."""
    contact = "binary_sensor.shelly_blu_door_window_b50c_window"
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(contact, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert calls
    assert all(c.data["entity_id"] == "climate.salotto_termostato_2" for c in calls)
    assert all(c.data["preset_mode"] == "building_protection" for c in calls)


async def test_unavailable_contact_never_pauses(hass):
    """BTHome contacts are battery/BLE: unavailable must be ignored (a dead
    battery never pauses a room)."""
    contact = "binary_sensor.gabri_room_finestra_g_window"
    await _setup(hass)
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(contact, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert calls == []


# --- v0.57.0: long-open alert (owner rule 3) ---------------------------------

ALERT_CONTACT = "binary_sensor.aaa_window"          # studio_v
ALERT_DELAY = timedelta(minutes=31)


def _alert_mocks(hass):
    async_mock_service(hass, "climate", "set_preset_mode")
    return (
        async_mock_service(hass, "notify", "mobile_app_matphone16"),
        async_mock_service(hass, "notify", "mobile_app_pixel_10"),
    )


async def test_long_open_contact_pages_both_phones_once(hass):
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()

    assert len(mattia) == 1 and len(ehi) == 1
    assert "Studio V" in mattia[0].data["message"]
    assert mattia[0].data["data"]["tag"] == "villa_hvac_window_open_studio_v"
    # once per episode: more time passes, no re-page
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()
    assert len(mattia) == 1

    # close + reopen -> a fresh episode may page again
    hass.states.async_set(ALERT_CONTACT, "off")
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 3)
    await hass.async_block_till_done()
    assert len(mattia) == 2 and len(ehi) == 2


async def test_close_before_threshold_never_pages(hass):
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()

    assert mattia == [] and ehi == []


async def test_vasistas_covers_never_page(hass):
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)

    hass.states.async_set(WINDOW, "open")           # cover.vasistas_gabriele
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()

    assert mattia == [] and ehi == []


async def test_deliberate_airing_suppresses_then_rearms(hass):
    """free_air ON = 'I opened the windows' — no paging while airing; the
    episode is RE-ARMED, not consumed: when the airing ends and the window is
    STILL open (the forgotten-window case rule 3 exists for), the next interval
    pages."""
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.free_air"}, blocking=True
    )
    await hass.async_block_till_done()

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert mattia == [] and ehi == []                # airing -> suppressed

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.free_air"}, blocking=True
    )
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()
    assert len(mattia) == 1 and len(ehi) == 1        # forgotten window pages


async def test_windows_free_cool_airing_suppresses_the_page(hass):
    """windows-free-cool armed + enough contacts open = deliberate airing."""
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.windows_free_cooling"}, blocking=True,
    )
    await hass.async_block_till_done()
    for contact in (
        ALERT_CONTACT,
        "binary_sensor.gabri_room_finestra_g_window",
        "binary_sensor.main_bedroom_finestra_piccola_bedroom_window",
    ):
        hass.states.async_set(contact, "on")         # 3 open >= default count 3
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()

    assert mattia == [] and ehi == []


async def test_ble_readvertisement_does_not_double_page(hass):
    """Repeated on->on state events (BLE re-advertisements) must neither
    re-schedule the timer nor page twice; an unavailable blip mid-episode
    neither cancels nor fires."""
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "on", {"rssi": -70})   # re-advert
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "unavailable")         # BLE blip
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "on")                  # back
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()

    assert len(mattia) == 1 and len(ehi) == 1        # exactly one page


async def test_unavailable_at_fire_rearms_instead_of_dropping(hass):
    """The contact reading unavailable exactly when the timer fires must not
    silently drop the episode — the alert re-arms and pages one interval later
    if the window is still open."""
    await _setup(hass)
    mattia, _ehi = _alert_mocks(hass)

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    hass.states.async_set(ALERT_CONTACT, "unavailable")  # blip right before fire
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert mattia == []                              # not paged, not dropped

    hass.states.async_set(ALERT_CONTACT, "on")       # sensor returns; still open
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()
    assert len(mattia) == 1                          # re-armed timer paged


async def test_forgotten_last_window_pages_after_airing_ends(hass):
    """The rule-3 core case: 3 windows open while windows-free-cool is armed
    (suppressed as deliberate airing), 2 get closed, ONE is forgotten — the
    re-armed timer pages for it once the count drops below the threshold."""
    await _setup(hass)
    mattia, ehi = _alert_mocks(hass)
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.windows_free_cooling"}, blocking=True,
    )
    await hass.async_block_till_done()
    others = (
        "binary_sensor.gabri_room_finestra_g_window",
        "binary_sensor.main_bedroom_finestra_piccola_bedroom_window",
    )
    for contact in (ALERT_CONTACT, *others):
        hass.states.async_set(contact, "on")
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert mattia == []                              # airing -> all suppressed

    for contact in others:                           # airing over, one forgotten
        hass.states.async_set(contact, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()

    assert len(mattia) == 1 and len(ehi) == 1
    assert "Studio V" in mattia[0].data["message"]


async def test_page_claims_pause_only_when_actually_engaged(hass):
    """Adversarial-review MAJOR: with the master (or Auto setback) off the room
    is NOT paused — the page must warn, not lie. With both on and the zone
    paused, it states the pause."""
    await _setup(hass)                               # master OFF
    mattia, _ehi = _alert_mocks(hass)
    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert "NON è in pausa" in mattia[0].data["message"]

    # Fresh episode with the supervisor on -> truthful pause claim.
    hass.states.async_set(ALERT_CONTACT, "off")
    await hass.async_block_till_done()
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 2)
    await hass.async_block_till_done()
    assert "il clima della stanza è in pausa" in mattia[-1].data["message"]


async def test_kitchen_door_page_names_the_door_not_salotto(hass):
    """Porta Cucina pauses the living_room LEADER, but the page must name the
    door (a 'Salotto window' message would send the reader to the wrong room)."""
    await _setup(hass)
    mattia, _ehi = _alert_mocks(hass)
    hass.states.async_set("binary_sensor.shelly_blu_door_window_b50c_window", "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert "Porta Cucina" in mattia[0].data["message"]
    assert "Salotto:" not in mattia[0].data["message"]


async def test_window_already_open_at_startup_arms_the_alert(hass):
    """A window open when HA boots must still page after the interval (the
    clock restarts from boot — the true opening time is unknown)."""
    hass.states.async_set(ALERT_CONTACT, "on")       # open BEFORE setup
    await _setup(hass)
    mattia, _ehi = _alert_mocks(hass)

    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY)
    await hass.async_block_till_done()
    assert len(mattia) == 1


async def test_alert_disabled_with_zero_minutes(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={},
        options={"window_alert_minutes": 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mattia, ehi = _alert_mocks(hass)

    hass.states.async_set(ALERT_CONTACT, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + ALERT_DELAY * 4)
    await hass.async_block_till_done()

    assert mattia == [] and ehi == []


async def test_close_restores_current_mode_preset(hass):
    await _setup(hass)  # mode defaults to Casa -> comfort
    await enable_supervisor(hass)
    seed_thermostats(hass, preset="comfort")
    calls = async_mock_service(hass, "climate", "set_preset_mode")

    hass.states.async_set(WINDOW, "open")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + WINDOW_OPEN_DELAY + timedelta(seconds=5))
    await hass.async_block_till_done()
    calls.clear()
    # Simulate the KNX thermostat actually applying building_protection.
    hass.states.async_set(CLIMATE, "cool", {"preset_mode": "building_protection"})

    hass.states.async_set(WINDOW, "closed")
    await hass.async_block_till_done()

    assert calls
    assert all(c.data["entity_id"] == CLIMATE for c in calls)
    assert all(c.data["preset_mode"] == "comfort" for c in calls)  # Casa preset
