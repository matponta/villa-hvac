"""v0.72.0 fancoil-unit actuator: re-arm / write order / provenance."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN, NIGHT_GUARD_FAN_PCT
from custom_components.villa_hvac.supervisor import (
    fan_lever,
    merge_desired_owned,
    switch_lever,
)
from custom_components.villa_hvac.supervisor.fan_actuator import (
    REARM_OWNER,
    FanLive,
    FanUnit,
    resolve_fan_units,
)

from .helpers import enable_supervisor, seed_thermostats

FAN = "fan.fancoil_studio_pianerottolo_p1"
MAN = "switch.fancoil_studio_pianerottolo_p1_manuale"
FK, SK = fan_lever(FAN), switch_lever(MAN)
UNIT = FanUnit(zone_id="office", fan=FAN, manuale=MAN)


def _resolve(desired, *, fan_on, manuale_on, turned_off=(), unit=UNIT, owners=None,
             held=frozenset()):
    return resolve_fan_units(
        desired, owners or {k: "ctrl" for k in desired}, [unit],
        {FAN: FanLive(fan_on=fan_on, manuale_on=manuale_on)},
        frozenset(turned_off), rearm_pct=33, held=frozenset(held),
    )


# --- merge provenance ---------------------------------------------------------

def test_merge_owned_first_opinion_wins_and_records_owner():
    merged, owners = merge_desired_owned([
        ("RackGuard", {"a": 1}),
        ("house_mode_policy", {"a": 2, "b": None}),
    ])
    assert merged == {"a": 1, "b": None}
    assert owners == {"a": "RackGuard", "b": "house_mode_policy"}


# --- I1 re-arm ----------------------------------------------------------------

def test_rearm_a_fan_we_switched_off_once_nobody_holds_it():
    res = _resolve({}, fan_on=False, manuale_on=False, turned_off={FAN})
    assert res.desired == {FK: 33}
    assert res.owners[FK] == REARM_OWNER
    assert res.rearmed == (FAN,)


def test_rearm_on_the_handback_cycle_itself():
    # a controller releases manuale this cycle and says nothing about the fan
    res = _resolve({SK: "off"}, fan_on=False, manuale_on=True, turned_off={FAN})
    assert res.desired[FK] == 33
    # I2: the fan is written BEFORE manuale goes off
    assert list(res.desired) == [FK, SK]


def test_no_rearm_for_a_fan_we_did_not_switch_off():
    # a human pressed OFF on the wall: not ours to fight (the watchdog, which is
    # temperature-gated, is the only thing allowed to revive that one)
    res = _resolve({}, fan_on=False, manuale_on=False, turned_off=())
    assert res.desired == {}


def test_no_rearm_while_someone_still_holds_the_fan():
    res = _resolve(
        {SK: "on", FK: 0}, fan_on=False, manuale_on=True, turned_off={FAN}
    )
    assert res.desired == {SK: "on", FK: 0}
    assert res.rearmed == ()


def test_no_rearm_while_the_unit_stays_manual():
    # manuale left ON (held outside our opinion set): a % would just hold, and
    # nobody asked — do not invent one
    res = _resolve({}, fan_on=False, manuale_on=True, turned_off={FAN})
    assert res.desired == {}


def test_rearm_deferred_while_paused_then_fires():
    paused = FanUnit(zone_id="office", fan=FAN, manuale=MAN, defer_rearm=True)
    res = _resolve({}, fan_on=False, manuale_on=False, turned_off={FAN}, unit=paused)
    assert res.desired == {} and res.deferred == (FAN,)
    res = _resolve({}, fan_on=False, manuale_on=False, turned_off={FAN})
    assert res.desired == {FK: 33}


def test_alive_is_confirmed_only_by_a_live_on_read():
    res = _resolve({}, fan_on=True, manuale_on=False, turned_off={FAN})
    assert res.confirmed_alive == frozenset({FAN})
    res = _resolve({}, fan_on=None, manuale_on=False, turned_off={FAN})
    assert res.confirmed_alive == frozenset() and res.desired == {}


def test_no_rearm_when_the_switch_is_conceded_to_a_human():
    """Review MINOR: a controller wanting manuale OFF while the human holds it ON
    (manual-hold) must not re-arm a fan the human silenced in manual."""
    res = _resolve({SK: "off"}, fan_on=False, manuale_on=True, turned_off={FAN},
                   held={SK})
    assert FK not in res.desired


def test_no_rearm_over_an_explicit_release_or_a_held_fan():
    assert _resolve({FK: None}, fan_on=False, manuale_on=False,
                    turned_off={FAN}).desired == {FK: None}
    assert _resolve({}, fan_on=False, manuale_on=False, turned_off={FAN},
                    held={FK}).desired == {}


# --- I2 order + I3 provenance ------------------------------------------------

def test_entering_manual_writes_switch_before_percentage():
    res = _resolve({FK: 50, "x": 1, SK: "on"}, fan_on=True, manuale_on=False)
    assert list(res.desired) == [SK, FK, "x"]


def test_unit_owner_labels():
    assert _resolve({}, fan_on=True, manuale_on=False).unit_owner[FAN] == "knx_auto"
    assert _resolve({}, fan_on=True, manuale_on=True).unit_owner[FAN] == "manual"
    res = _resolve({SK: "on", FK: 70}, fan_on=True, manuale_on=False,
                   owners={SK: "P1GuardController", FK: "P1GuardController"})
    assert res.unit_owner[FAN] == "P1GuardController"


def test_percentage_into_auto_is_flagged():
    res = _resolve({FK: 60}, fan_on=True, manuale_on=False)
    assert res.notes[FAN] == "pct_in_auto"
    res = _resolve({SK: "off", FK: 60}, fan_on=True, manuale_on=True)
    assert res.notes[FAN] == "handback"


# --- engine integration ------------------------------------------------------

async def test_engine_rearms_a_stranded_supervisor_fan_until_confirmed(hass):
    seed_thermostats(hass)
    hass.states.async_set(FAN, "off", {"percentage": 0})
    hass.states.async_set(MAN, "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await enable_supervisor(hass)
    for svc in ("set_preset_mode", "set_temperature"):
        async_mock_service(hass, "climate", svc)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    fan_on = async_mock_service(hass, "fan", "turn_on")
    engine = entry.runtime_data.engine
    engine._fans_turned_off.add(FAN)           # an earlier silence of ours

    await engine._run()
    assert [c.data for c in fan_on if c.data["entity_id"] == FAN] == [
        {"entity_id": FAN, "percentage": NIGHT_GUARD_FAN_PCT}
    ]
    assert FAN in engine._fans_turned_off       # written, not yet confirmed
    assert engine.fan_owners[FAN] == REARM_OWNER

    hass.states.async_set(FAN, "on", {"percentage": 33})
    await engine._run()
    assert FAN not in engine._fans_turned_off   # live read confirmed it alive
    assert engine.fan_owners[FAN] == "knx_auto"


async def test_engine_defers_rearm_in_vacanza(hass):
    """Review MINOR: like the stranded-fan watchdog, never spin a fan in an empty
    deep-setback house (Vacanza: mode_offset None, building_protection)."""
    seed_thermostats(hass)
    hass.states.async_set(FAN, "off", {"percentage": 0})
    hass.states.async_set(MAN, "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await enable_supervisor(hass)
    for svc in ("set_preset_mode", "set_temperature"):
        async_mock_service(hass, "climate", svc)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    fan_on = async_mock_service(hass, "fan", "turn_on")
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.house_mode", "option": "Vacanza"}, blocking=True,
    )
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    engine._fans_turned_off.add(FAN)
    await engine._run()
    assert not [c for c in fan_on if c.data["entity_id"] == FAN]
    assert FAN in engine.fan_deferred and FAN in engine._fans_turned_off
