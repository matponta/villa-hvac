"""Tests for the v0.86.0 VMC year-round core (STORY_VMC.md): humidity-driven
speed on the ground unit, the damp-air flush veto, the quiet 66 % night flush,
the winter boost cap, ownership hand-back/concede/orphans, and the alerts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    DOMAIN,
    OUTDOOR_RH,
    OUTDOOR_TEMP,
    SEASON_REFERENCE_CLIMATE,
    VMC_SPEED_MIN_HOLD,
    VMC_WINTER_BOOST_MAX,
)
from custom_components.villa_hvac.supervisor.ventilation import (
    REASON_BASE,
    REASON_CELLAR_DRY,
    REASON_DAMP_OUTSIDE,
    REASON_DEHUMIDIFY,
    REASON_DEHUMIDIFY_HIGH,
    REASON_NO_DATA,
    REASON_QUIET_FLUSH,
    Held,
    RoomAir,
    absolute_humidity,
    bypass_state,
    hold_value,
    recovery_efficiency,
    vmc_dehumidify,
    vmc_speed_target,
)

GROUND_FAN = "fan.10_5_150_27"
LIVING_FAN = "fan.vmc_cucina_e_casa"
GROUND_BOOST = "switch.10_5_150_27_boost"
LIVING_BOOST = "switch.vmc_boost"
PALESTRA_RH = "sensor.everything_presence_one_3febdc_humidity"
CANTINA_RH = "sensor.everything_presence_one_a8c934_humidity"


# --- pure: absolute humidity ---------------------------------------------------

def test_absolute_humidity_reference_points():
    assert absolute_humidity(20.0, 50.0) == pytest.approx(8.63, abs=0.05)
    assert absolute_humidity(8.0, 90.0) == pytest.approx(7.42, abs=0.05)
    # the point of the module: 90 % outside in autumn is DRIER than a 50 % room
    assert absolute_humidity(8.0, 90.0) < absolute_humidity(20.0, 50.0)


@pytest.mark.parametrize("t,rh", [(None, 50), (20, None), (20, -1), (20, 101),
                                  (float("nan"), 50)])
def test_absolute_humidity_rejects_bad_input(t, rh):
    assert absolute_humidity(t, rh) is None


# --- pure: dehumidify --------------------------------------------------------

def _dh(rooms, *, out_t=8.0, out_rh=80.0, active=False):
    return vmc_dehumidify(
        rooms, outdoor_temp=out_t, outdoor_rh=out_rh, active=active,
        margin=1.0, hysteresis=0.5, release_delta=5.0, rh_very_high=75.0,
    )


def _palestra(rh, t=18.0):
    return RoomAir("palestra", t, rh, 65.0)


def test_dries_a_damp_room_with_drier_outside_air():
    d = _dh([_palestra(70.0)])
    assert (d.pct, d.reason, d.zone, d.damp_outside) == (
        66, REASON_DEHUMIDIFY, "palestra", False,
    )


def test_wetter_outside_air_never_dries_and_flags_damp():
    # a humid summer evening: 25 °C / 70 % holds far more water than 18 °C / 70 %
    d = _dh([_palestra(70.0)], out_t=25.0, out_rh=70.0)
    assert d.pct is None and d.reason == REASON_DAMP_OUTSIDE and d.damp_outside


def test_damp_outside_is_reported_even_below_the_ceiling():
    """It is also the night-flush veto, which must not wait for a damp room."""
    d = _dh([_palestra(55.0)], out_t=25.0, out_rh=70.0)
    assert d.pct is None and d.reason == REASON_BASE and d.damp_outside


def test_margin_and_hysteresis_on_the_absolute_gap():
    room = _palestra(70.0, t=18.0)                     # ~10.7 g/m³
    near = 9.95                                       # gap 0.75 g/m³
    # pick an outdoor RH at 18 °C giving that AH
    rh = near / absolute_humidity(18.0, 100.0) * 100.0
    assert _dh([room], out_t=18.0, out_rh=rh).reason == REASON_DAMP_OUTSIDE
    assert _dh([room], out_t=18.0, out_rh=rh, active=True).pct == 66


def test_release_band_keeps_drying_while_active():
    assert _dh([_palestra(62.0)]).pct is None                 # not started
    assert _dh([_palestra(62.0)], active=True).pct == 66      # still drying
    assert _dh([_palestra(59.0)], active=True).pct is None    # released


def test_very_high_runs_full_speed():
    d = _dh([_palestra(80.0)])
    assert (d.pct, d.reason) == (100, REASON_DEHUMIDIFY_HIGH)


def test_dry_cellar_caps_but_never_vetoes():
    cellar = RoomAir("cantina_vini", 16.0, 45.0, 65.0, 55.0)
    assert _dh([_palestra(80.0), cellar]).pct == 66
    assert _dh([_palestra(80.0), cellar]).reason == REASON_CELLAR_DRY
    assert _dh([_palestra(70.0), cellar]).pct == 66


def test_driving_room_is_the_one_furthest_over_its_ceiling():
    cellar = RoomAir("cantina_vini", 16.0, 68.0, 65.0, 55.0)   # +3
    d = _dh([_palestra(66.0), cellar])                          # +1
    assert d.zone == "cantina_vini"


def test_no_data():
    assert _dh([RoomAir("palestra", None, 70.0, 65.0)]).reason == REASON_NO_DATA
    d = _dh([_palestra(70.0)], out_rh=None)
    assert d.pct is None and d.reason == REASON_NO_DATA and not d.damp_outside


# --- pure: speed target --------------------------------------------------------

def test_speed_target():
    assert vmc_speed_target(dehumidify_pct=None, flush_wanted=False, quiet=False,
                            quiet_pct=66) == (None, REASON_BASE)
    assert vmc_speed_target(dehumidify_pct=None, flush_wanted=True, quiet=True,
                            quiet_pct=66) == (66, REASON_QUIET_FLUSH)
    # a flush without the quiet veto is the boost's job, not the speed's
    assert vmc_speed_target(dehumidify_pct=None, flush_wanted=True, quiet=False,
                            quiet_pct=66) == (None, REASON_BASE)
    assert vmc_speed_target(dehumidify_pct=100, flush_wanted=True, quiet=True,
                            quiet_pct=66) == (100, REASON_DEHUMIDIFY_HIGH)


# --- pure: recovery + hold -----------------------------------------------------

def test_recovery_efficiency_and_bypass():
    eff = recovery_efficiency(5.0, 17.0, 20.0, min_spread=4.0)
    assert eff == pytest.approx(0.8) and bypass_state(eff) == "chiuso"
    eff = recovery_efficiency(15.0, 15.2, 24.0, min_spread=4.0)
    assert bypass_state(eff) == "aperto"
    assert recovery_efficiency(18.0, 19.0, 20.0, min_spread=4.0) is None
    assert bypass_state(None) is None


def test_hold_value_survives_a_blip_only():
    t0 = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    h = hold_value(None, 20.0, t0, max_age=timedelta(minutes=10))
    assert h == Held(20.0, t0)
    assert hold_value(h, None, t0 + timedelta(minutes=9),
                      max_age=timedelta(minutes=10)) == h
    assert hold_value(h, None, t0 + timedelta(minutes=11),
                      max_age=timedelta(minutes=10)) is None


# --- integration -----------------------------------------------------------------

async def _setup(hass, *, season="cool", storage=None):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, season, {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _fans(hass, ground=33, living=33):
    hass.states.async_set(GROUND_FAN, "on", {"percentage": ground})
    hass.states.async_set(LIVING_FAN, "on", {"percentage": living})


def _arm(hass, entry, *, palestra_rh=70.0, out_t=8.0, out_rh=80.0):
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.vmc_auto", "on")
    hass.states.async_set(OUTDOOR_TEMP, str(out_t))
    hass.states.async_set(OUTDOOR_RH, str(out_rh))
    hass.states.async_set(PALESTRA_RH, str(palestra_rh))
    hass.states.async_set(CANTINA_RH, "57")   # inside the wine band
    entry.runtime_data.data = {"zone_temps": {
        "palestra": {"value": 18.0},
        "cantina_vini": {"value": 16.0},
        "kitchen": {"value": 21.0},
        "main_bedroom": {"value": 21.0},
    }}


def _pcts(calls, fan):
    return [c.data["percentage"] for c in calls if c.data["entity_id"] == fan]


async def test_ground_dries_then_hands_back_the_owner_speed(hass):
    with freeze_time("2026-10-28 12:00:00") as frozen:
        entry = await _setup(hass, season="heat")
        vmc = entry.runtime_data.vmc
        sets = async_mock_service(hass, "fan", "set_percentage")
        _fans(hass, ground=33)
        _arm(hass, entry, palestra_rh=70.0)

        await vmc._evaluate()
        assert _pcts(sets, GROUND_FAN) == [66]
        assert _pcts(sets, LIVING_FAN) == []          # no humidity role
        assert vmc.view["ground"]["reason"] == REASON_DEHUMIDIFY
        hass.states.async_set(GROUND_FAN, "on", {"percentage": 66})

        await vmc._evaluate()                          # edge-only
        assert _pcts(sets, GROUND_FAN) == [66]

        hass.states.async_set(PALESTRA_RH, "55")       # dried out
        await vmc._evaluate()
        assert _pcts(sets, GROUND_FAN) == [66]         # min hold
        frozen.tick(VMC_SPEED_MIN_HOLD)
        await vmc._evaluate()
        assert _pcts(sets, GROUND_FAN) == [66, 33]     # owner's speed back
        assert vmc._speed == {}


async def test_summer_flush_vetoed_on_ground_when_outside_is_wetter(hass):
    entry = await _setup(hass)
    vmc = entry.runtime_data.vmc
    on = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "fan", "set_percentage")
    _fans(hass)
    # 20 °C / 95 % outside: cool enough to flush, but wetter than an 18 °C room
    _arm(hass, entry, palestra_rh=60.0, out_t=20.0, out_rh=95.0)
    entry.runtime_data.data["zone_temps"]["palestra"] = {"value": 24.0}
    entry.runtime_data.data["zone_temps"]["kitchen"] = {"value": 24.0}
    hass.states.async_set("select.house_mode", "Casa")

    await vmc._evaluate()

    assert not [c for c in on if c.data["entity_id"] == GROUND_BOOST]
    assert [c for c in on if c.data["entity_id"] == LIVING_BOOST]
    assert vmc.view["ground"]["damp_outside"] is True


async def test_occupied_notte_flushes_the_bedroom_unit_at_mid_speed(hass):
    entry = await _setup(hass)
    vmc = entry.runtime_data.vmc
    on = async_mock_service(hass, "switch", "turn_on")
    sets = async_mock_service(hass, "fan", "set_percentage")
    _fans(hass)
    _arm(hass, entry, palestra_rh=50.0, out_t=18.0, out_rh=60.0)
    entry.runtime_data.data["zone_temps"]["main_bedroom"] = {"value": 26.0}
    hass.states.async_set("select.house_mode", "Notte")
    for p in ("person.mattia_pontacolone", "person.ehi"):
        hass.states.async_set(p, "home")

    await vmc._evaluate()

    assert not [c for c in on if c.data["entity_id"] == LIVING_BOOST]
    assert _pcts(sets, LIVING_FAN) == [66]
    assert vmc.view["living"]["reason"] == REASON_QUIET_FLUSH


async def test_a_hand_change_is_conceded_not_fought(hass):
    with freeze_time("2026-10-28 12:00:00") as frozen:
        entry = await _setup(hass, season="heat")
        vmc = entry.runtime_data.vmc
        sets = async_mock_service(hass, "fan", "set_percentage")
        _fans(hass)
        _arm(hass, entry, palestra_rh=70.0)
        await vmc._evaluate()
        assert _pcts(sets, GROUND_FAN) == [66]

        frozen.tick(timedelta(minutes=5))              # past the write grace
        hass.states.async_set(GROUND_FAN, "on", {"percentage": 100})  # owner
        await vmc._evaluate()
        await vmc._evaluate()
        assert "ground" not in vmc._speed
        hass.states.async_set(PALESTRA_RH, "50")
        frozen.tick(VMC_SPEED_MIN_HOLD)
        await vmc._evaluate()
        assert _pcts(sets, GROUND_FAN) == [66]         # no write-back


async def test_disable_restores_the_snapshot(hass):
    entry = await _setup(hass, season="heat")
    vmc = entry.runtime_data.vmc
    sets = async_mock_service(hass, "fan", "set_percentage")
    _fans(hass)
    _arm(hass, entry, palestra_rh=70.0)
    await vmc._evaluate()
    hass.states.async_set("switch.vmc_auto", "off")
    await vmc._evaluate()
    assert _pcts(sets, GROUND_FAN) == [66, 33]


async def test_a_pre_restart_raise_is_handed_back(hass, hass_storage):
    hass_storage["villa_hvac_vmc"] = {
        "version": 1, "key": "villa_hvac_vmc",
        "data": {"speed": {"ground": {"target": 66, "snapshot": 33}}},
    }
    entry = await _setup(hass, season="heat")
    vmc = entry.runtime_data.vmc
    sets = async_mock_service(hass, "fan", "set_percentage")
    _fans(hass, ground=66)                              # still at what we set

    await vmc._evaluate()                               # master off -> release

    assert _pcts(sets, GROUND_FAN) == [33]
    assert vmc._speed == {} and vmc._orphans == {}


async def test_a_pre_restart_raise_changed_since_is_forgotten(hass, hass_storage):
    hass_storage["villa_hvac_vmc"] = {
        "version": 1, "key": "villa_hvac_vmc",
        "data": {"speed": {"ground": {"target": 66, "snapshot": 33}}},
    }
    entry = await _setup(hass, season="heat")
    vmc = entry.runtime_data.vmc
    sets = async_mock_service(hass, "fan", "set_percentage")
    _fans(hass, ground=100)                             # someone changed it

    await vmc._evaluate()

    assert _pcts(sets, GROUND_FAN) == []


async def test_winter_boost_cap(hass):
    with freeze_time("2026-10-28 12:00:00") as frozen:
        entry = await _setup(hass, season="heat")
        vmc = entry.runtime_data.vmc
        off = async_mock_service(hass, "switch", "turn_off")
        async_mock_service(hass, "fan", "set_percentage")
        _fans(hass)
        _arm(hass, entry, palestra_rh=50.0)
        hass.states.async_set(LIVING_BOOST, "on")       # someone's long boost

        await vmc._evaluate()
        frozen.tick(VMC_WINTER_BOOST_MAX - timedelta(minutes=1))
        await vmc._evaluate()
        assert not [c for c in off if c.data["entity_id"] == LIVING_BOOST]
        frozen.tick(timedelta(minutes=1))
        await vmc._evaluate()
        assert [c for c in off if c.data["entity_id"] == LIVING_BOOST]


async def test_no_winter_boost_cap_in_summer(hass):
    with freeze_time("2026-07-28 12:00:00") as frozen:
        entry = await _setup(hass)
        vmc = entry.runtime_data.vmc
        off = async_mock_service(hass, "switch", "turn_off")
        _fans(hass)
        _arm(hass, entry, palestra_rh=50.0, out_t=30.0, out_rh=40.0)
        hass.states.async_set(LIVING_BOOST, "on")
        await vmc._evaluate()
        frozen.tick(VMC_WINTER_BOOST_MAX * 2)
        await vmc._evaluate()
        assert not [c for c in off if c.data["entity_id"] == LIVING_BOOST]


async def test_filter_alert_pages_once_per_episode(hass):
    with freeze_time("2026-10-28 12:00:00"):
        entry = await _setup(hass, season="heat")
        vmc = entry.runtime_data.vmc
        sent = async_mock_service(hass, "notify", "mobile_app_matphone16")
        async_mock_service(hass, "notify", "mobile_app_pixel_10")
        _fans(hass)
        hass.states.async_set("sensor.10_5_150_27_filter_days_left",
                              "2026-10-28T11:00:00+00:00")
        hass.states.async_set("sensor.vmc_cucina_e_casa_filter_days_left",
                              "2027-02-01T00:00:00+00:00")
        await vmc._evaluate()
        await vmc._evaluate()
        titles = [c.data["title"] for c in sent]
        assert titles == ["VMC Bagno Cantine: filtro da cambiare"]


async def test_alerts_off_sends_nothing(hass):
    entry = await _setup(hass, season="heat")
    vmc = entry.runtime_data.vmc
    sent = async_mock_service(hass, "notify", "mobile_app_matphone16")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.vmc_alerts"},
        blocking=True,
    )
    hass.states.async_set("sensor.10_5_150_27_filter_days_left",
                          "2020-01-01T00:00:00+00:00")
    await vmc._evaluate()
    assert not sent


async def test_status_and_air_sensors_exist(hass):
    entry = await _setup(hass, season="heat")
    vmc = entry.runtime_data.vmc
    hass.states.async_set("sensor.vmc_cucina_e_casa_outdoor_air_temperature", "5.0")
    hass.states.async_set("sensor.vmc_cucina_e_casa_supply_air_temperature", "17.0")
    hass.states.async_set("sensor.vmc_cucina_e_casa_extract_air_temperature", "20.0")
    await vmc._evaluate()
    assert vmc.view["living"]["recovery_efficiency"] == pytest.approx(0.8)
    assert vmc.view["living"]["reason"] == "non_attivo"
    ids = hass.states.async_entity_ids("sensor")
    assert any("vmc_cucina_e_casa_stato" in i for i in ids)
    assert any("vmc_bagno_cantine_recupero_calore" in i for i in ids)
