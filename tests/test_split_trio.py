"""Tests for the split-AC trio (#6) — P0 observe slice.

P0 adds no controller: it plumbs the three Daikin heads into the snapshot, the
new `hvac_mode:` / `fan_mode:` lever kinds (read + dispatch), the observe view
(`engine.split_view` -> `sensor.hvac_split`), and the heat↔cool conflict detector
the KLIC-DD bus cannot itself report. Nothing actuates the splits yet.
"""
from __future__ import annotations

import datetime as dt

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_HOME,
    HOUSE_MODE_VACATION,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.villa_hvac.engine import SupervisorEngine, build_house_state
from custom_components.villa_hvac.policies import SplitGroupController
from custom_components.villa_hvac.supervisor import (
    HouseState,
    LeverState,
    ZoneSnapshot,
    fan_mode_lever,
    hvac_mode_lever,
    split_dwell,
    split_head_target,
    split_members,
    split_mode_conflict,
    temperature_lever,
)

SALOTTO = "climate.salotto_termostato_2"
CANTINA = "climate.aircon_cantina_vini_2"
PALESTRA_SPLIT = "climate.aircon_palestra_2"
GARAGE = "climate.aircon_garage_2"


def _head(zid, entity, mode, *, sp=None, temp=None, fan="low"):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=entity, emitter="split_ac",
        ac_group="split_trio", split_climate=entity, split_mode=mode,
        split_setpoint=sp, split_temp=temp, split_fan_mode=fan,
    )


# --- pure: lever keys + group helpers ----------------------------------------

def test_split_lever_keys():
    assert hvac_mode_lever(CANTINA) == f"hvac_mode:{CANTINA}"
    assert fan_mode_lever(CANTINA) == f"fan_mode:{CANTINA}"


def test_split_members_filters_by_group_and_orders():
    now = dt.datetime(2026, 7, 9, 12, 0)
    zones = {
        "cantina_vini": _head("cantina_vini", CANTINA, "cool"),
        "salotto": ZoneSnapshot(
            zone_id="salotto", name="Salotto", climate=SALOTTO, emitter="fancoil"
        ),
        "garage": _head("garage", GARAGE, "off"),
    }
    st = HouseState(now=now, zones=zones)
    assert [z.zone_id for z in split_members(st)] == ["cantina_vini", "garage"]
    assert [z.zone_id for z in split_members(st, "other")] == []


def test_split_mode_conflict_only_heat_vs_cool():
    # cool + heat on one shared compressor is physically unsatisfiable.
    assert split_mode_conflict([_head("a", CANTINA, "cool"), _head("b", GARAGE, "heat")])
    # dry is the same refrigerant direction as cool -> compatible (KLIC-DD docs).
    assert not split_mode_conflict([_head("a", CANTINA, "cool"), _head("b", GARAGE, "dry")])
    # fan_only needs no compressor -> neutral, never conflicts.
    assert not split_mode_conflict([_head("a", CANTINA, "heat"), _head("b", GARAGE, "fan_only")])
    # off is neutral.
    assert not split_mode_conflict([_head("a", CANTINA, "cool"), _head("b", GARAGE, "off")])


# --- engine: snapshot population + lever I/O + observe view -------------------

async def _setup(hass):
    hass.states.async_set(SALOTTO, "cool", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_build_house_state_populates_split_fields(hass):
    entry = await _setup(hass)
    hass.states.async_set(
        CANTINA, "cool",
        {"temperature": 19.0, "current_temperature": 22.3, "fan_mode": "low"},
    )
    hass.states.async_set(PALESTRA_SPLIT, "off", {"fan_mode": "off"})
    hass.states.async_set(GARAGE, "heat", {"temperature": 23.0})

    state = build_house_state(hass, entry, entry.runtime_data)

    cant = state.zones["cantina_vini"]
    assert cant.ac_group == "split_trio"
    assert cant.split_climate == CANTINA
    assert cant.split_mode == "cool"
    assert cant.split_setpoint == 19.0
    assert cant.split_temp == 22.3
    assert cant.split_fan_mode == "low"

    # palestra: the head is the explicit split_climate, NOT its radiant `climate`.
    pal = state.zones["palestra"]
    assert pal.split_climate == PALESTRA_SPLIT
    assert pal.split_mode == "off"

    # a non-split zone carries no split state.
    assert state.zones["living_room"].ac_group is None
    assert state.zones["living_room"].split_climate is None


async def test_split_view_reports_conflict_and_stays_dark(hass):
    entry = await _setup(hass)
    # cantina cooling + garage heating = a heat↔cool conflict the bus can't flag.
    hass.states.async_set(CANTINA, "cool", {"temperature": 19.0})
    hass.states.async_set(PALESTRA_SPLIT, "off", {})
    hass.states.async_set(GARAGE, "heat", {"temperature": 23.0})

    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[], controllers=[])
    await engine._run()

    view = engine.split_view
    assert view is not None
    assert view["enabled"] is False          # opt-in switch off (deploy-dark)
    assert view["conflict"] is True
    assert view["direction"] == "conflict"
    assert set(view["heads"]) == {"palestra", "cantina_vini", "garage"}
    assert view["heads"]["cantina_vini"]["mode"] == "cool"


async def test_hvac_mode_and_fan_mode_dispatch_via_arbiter(hass):
    entry = await _setup(hass)
    hass.states.async_set(CANTINA, "off", {"fan_mode": "off"})
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    fan_calls = async_mock_service(hass, "climate", "set_fan_mode")

    def policy(_state):
        return {hvac_mode_lever(CANTINA): "cool", fan_mode_lever(CANTINA): "low"}

    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[policy])
    await engine._run()

    assert len(mode_calls) == 1
    assert mode_calls[0].data["entity_id"] == CANTINA
    assert mode_calls[0].data["hvac_mode"] == "cool"
    assert len(fan_calls) == 1
    assert fan_calls[0].data["fan_mode"] == "low"


async def test_hvac_mode_idempotent_when_satisfied(hass):
    entry = await _setup(hass)
    hass.states.async_set(CANTINA, "cool", {"fan_mode": "low"})
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    def policy(_state):
        return {hvac_mode_lever(CANTINA): "cool"}  # already cool

    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[policy])
    await engine._run()
    assert len(mode_calls) == 0


# --- P1: pure role + dwell ---------------------------------------------------

def _tgt(role, **kw):
    args = dict(
        summer=True, home=True, occupied=True, temp=20.0, rh=60.0,
        cantina_setpoint=19.0, comfort_setpoint=24.0, rh_ceiling=65.0, rh_floor=55.0,
    )
    args.update(kw)
    return split_head_target(role, **args)


def test_split_head_target_roles():
    # storage (cantina): cool @ setpoint at normal RH, regardless of season/occupancy.
    assert _tgt("storage", summer=False, home=False, occupied=False) == ("cool", 19.0)
    # comfort (palestra): cool only in summer + home + occupied.
    assert _tgt("comfort") == ("cool", 24.0)
    for kw in ({"summer": False}, {"home": False}, {"occupied": False}):
        assert _tgt("comfort", **kw) == ("off", None)
    # manual (garage) / unknown -> never commanded.
    assert _tgt("manual") is None


def test_split_storage_humidity_handling():
    # too humid + already cool enough -> dedicated `dry` (no setpoint).
    assert _tgt("storage", rh=70.0, temp=18.5) == ("dry", None)
    # too humid + warm -> `cool` (cools AND dehumidifies).
    assert _tgt("storage", rh=70.0, temp=25.0) == ("cool", 19.0)
    # dry cellar (RH < floor) -> relax the setpoint warmer so it dries less.
    assert _tgt("storage", rh=44.0, temp=25.0) == ("cool", 19.0 + 1.5)
    # RH sensor stale (None) -> temp-only cool at the setpoint (never chase humidity).
    assert _tgt("storage", rh=None, temp=25.0) == ("cool", 19.0)
    # normal band -> plain self-regulating cool.
    assert _tgt("storage", rh=60.0, temp=25.0) == ("cool", 19.0)


def test_split_dwell_debounces_transitions():
    t0 = dt.datetime(2026, 7, 9, 12, 0)
    min_on, min_off = dt.timedelta(minutes=5), dt.timedelta(minutes=3)
    # first observation commits immediately.
    on, since = split_dwell(True, None, t0, min_on, min_off)
    assert on is True and since == t0
    # same state keeps the original `since` (no reset).
    assert split_dwell(True, (True, t0), t0 + dt.timedelta(minutes=10), min_on, min_off) == (True, t0)
    # on->off held until min_on elapses.
    assert split_dwell(False, (True, t0), t0 + dt.timedelta(minutes=2), min_on, min_off) == (True, t0)
    off, s = split_dwell(False, (True, t0), t0 + dt.timedelta(minutes=6), min_on, min_off)
    assert off is False and s == t0 + dt.timedelta(minutes=6)
    # off->on held until min_off elapses.
    assert split_dwell(True, (False, t0), t0 + dt.timedelta(minutes=1), min_on, min_off) == (False, t0)
    on2, _ = split_dwell(True, (False, t0), t0 + dt.timedelta(minutes=4), min_on, min_off)
    assert on2 is True


# --- P1: SplitGroupController ------------------------------------------------

def _split_house(*, enabled=True, summer=True, mode=HOUSE_MODE_HOME, palestra_occupied=True,
                 cantina_temp=None, cantina_rh=60.0, palestra_sp=24.0, house_sp=24.0):
    now = dt.datetime(2026, 7, 9, 12, 0)
    zones = {
        "cantina_vini": ZoneSnapshot(
            zone_id="cantina_vini", name="Cantina", climate=CANTINA, emitter="split_ac",
            ac_group="split_trio", split_climate=CANTINA, split_role="storage",
            split_temp=cantina_temp, humidity=cantina_rh,
        ),
        "palestra": ZoneSnapshot(
            zone_id="palestra", name="Palestra", climate="climate.palestra_termostato_2",
            emitter="radiant", ac_group="split_trio", split_climate=PALESTRA_SPLIT,
            split_role="comfort", occupied=palestra_occupied,
        ),
        "garage": ZoneSnapshot(
            zone_id="garage", name="Garage", climate=GARAGE, emitter="split_ac",
            ac_group="split_trio", split_climate=GARAGE, split_role="manual",
        ),
    }
    return HouseState(
        now=now, zones=zones, split_enabled=enabled,
        season=SEASON_SUMMER if summer else SEASON_WINTER,
        house_mode=mode, house_setpoint=house_sp, split_cantina_setpoint=19.0,
        split_palestra_setpoint=palestra_sp,
        split_min_on=dt.timedelta(minutes=5), split_min_off=dt.timedelta(minutes=3),
    )


def test_split_controller_yields_when_disabled():
    ctrl = SplitGroupController()
    assert ctrl(_split_house(enabled=False)) == {}


def test_split_controller_cantina_cool_palestra_occupied_garage_untouched():
    out = SplitGroupController()(_split_house())
    # cantina: cool @ 19 + fan (mode -> setpoint -> fan order).
    assert out[hvac_mode_lever(CANTINA)] == "cool"
    assert out[temperature_lever(CANTINA)] == 19.0
    assert fan_mode_lever(CANTINA) in out
    # palestra: cool @ house setpoint (summer + home + occupied).
    assert out[hvac_mode_lever(PALESTRA_SPLIT)] == "cool"
    assert out[temperature_lever(PALESTRA_SPLIT)] == 24.0
    # garage (manual): NEVER commanded.
    assert hvac_mode_lever(GARAGE) not in out
    # the controller only ever emits cool-side -> no head is ever driven to heat.
    assert "heat" not in out.values()


def test_split_controller_palestra_off_when_empty_and_cantina_still_cools():
    out = SplitGroupController()(_split_house(palestra_occupied=False))
    assert out[hvac_mode_lever(PALESTRA_SPLIT)] == "off"
    assert temperature_lever(PALESTRA_SPLIT) not in out   # no setpoint when off
    assert out[hvac_mode_lever(CANTINA)] == "cool"        # cantina unaffected


def test_split_controller_palestra_off_in_winter_and_away():
    assert SplitGroupController()(_split_house(summer=False))[hvac_mode_lever(PALESTRA_SPLIT)] == "off"
    assert SplitGroupController()(_split_house(mode=HOUSE_MODE_AWAY))[hvac_mode_lever(PALESTRA_SPLIT)] == "off"
    assert SplitGroupController()(_split_house(mode=HOUSE_MODE_VACATION))[hvac_mode_lever(PALESTRA_SPLIT)] == "off"


def test_split_controller_palestra_uses_own_setpoint():
    # palestra cools to its OWN comfort setpoint, decoupled from the house slider.
    out = SplitGroupController()(_split_house(palestra_sp=22.0, house_sp=26.0))
    assert out[hvac_mode_lever(PALESTRA_SPLIT)] == "cool"
    assert out[temperature_lever(PALESTRA_SPLIT)] == 22.0


def test_split_controller_cantina_dry_when_humid():
    # cellar cool enough but too humid -> dedicated `dry`, no setpoint written.
    out = SplitGroupController()(_split_house(cantina_temp=18.5, cantina_rh=70.0))
    assert out[hvac_mode_lever(CANTINA)] == "dry"
    assert temperature_lever(CANTINA) not in out
    # still cool-side only (dry is compatible with cool) -> never a conflict.
    assert "heat" not in out.values()


def test_split_controller_cantina_relaxes_setpoint_when_dry():
    # dry cellar (44%) -> warmer setpoint so the compressor dries the wine less.
    out = SplitGroupController()(_split_house(cantina_temp=25.0, cantina_rh=44.0))
    assert out[hvac_mode_lever(CANTINA)] == "cool"
    assert out[temperature_lever(CANTINA)] == 20.5   # 19 + 1.5 relax


# --- P1: fail-safe hand-back -------------------------------------------------

async def test_split_fail_safe_hands_back_managed_only(hass):
    entry = await _setup(hass)
    hass.states.async_set(CANTINA, "cool", {"temperature": 19.0})
    hass.states.async_set(PALESTRA_SPLIT, "cool", {"temperature": 24.0})
    async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "climate", "set_preset_mode")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    temp_calls = async_mock_service(hass, "climate", "set_temperature")

    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[], controllers=[])
    # Pretend we were managing cantina + palestra (committed hvac_mode levers).
    engine._lever_states = {
        hvac_mode_lever(CANTINA): LeverState(written="cool"),
        hvac_mode_lever(PALESTRA_SPLIT): LeverState(written="cool"),
    }
    await engine.async_fail_safe()

    by_entity = {c.data["entity_id"]: c.data["hvac_mode"] for c in mode_calls}
    assert by_entity[CANTINA] == "cool"        # wine = self-regulating dead-man
    assert by_entity[PALESTRA_SPLIT] == "off"  # comfort head off
    # cantina left at its storage setpoint.
    assert any(c.data["entity_id"] == CANTINA and c.data["temperature"] == 19.0
               for c in temp_calls)
    # lever state cleared so a queued cycle can't re-assert.
    assert hvac_mode_lever(CANTINA) not in engine._lever_states


async def test_split_fail_safe_noop_when_never_managed(hass):
    entry = await _setup(hass)
    hass.states.async_set(CANTINA, "off", {})
    async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "climate", "set_preset_mode")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    engine = SupervisorEngine(hass, entry, entry.runtime_data, policies=[], controllers=[])
    engine._lever_states = {}   # deploy-dark: we never touched a split
    await engine.async_fail_safe()

    assert mode_calls == []   # untouched splits are never actuated on fail-safe
