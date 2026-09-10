"""Tests for #5 VMC boost (night free-cooling ventilation)."""
from __future__ import annotations

from datetime import datetime, timedelta

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import (
    DOMAIN,
    OUTDOOR_TEMP,
    VMC_BOOST_COOLDOWN,
    VMC_BOOST_MAX_ON,
    VMC_BOOST_MIN_ON,
)
from custom_components.villa_hvac.supervisor import (
    VmcBoostState,
    vmc_boost_decision,
    vmc_boost_step,
)

GROUND = "switch.10_5_150_27_boost"
LIVING = "switch.vmc_boost"


# --- pure decision -----------------------------------------------------------

def _dec(**kw):
    base = dict(
        is_summer=True, outdoor=20.0, indoor=25.0, on_now=False,
        outdoor_max=24.0, margin=2.0, hysteresis=0.5,
    )
    base.update(kw)
    return vmc_boost_decision(**base)


def test_boost_when_cool_enough_outside():
    assert _dec(outdoor=20.0, indoor=25.0) is True   # 5 °C gap >= 2


def test_no_boost_out_of_season():
    assert _dec(is_summer=False) is False


def test_no_boost_when_outside_not_cool():
    assert _dec(outdoor=24.0) is False               # at the cap
    assert _dec(outdoor=25.0) is False               # above the cap


def test_no_boost_when_gap_too_small():
    assert _dec(outdoor=23.5, indoor=25.0, on_now=False) is False  # 1.5 < 2


def test_hysteresis_keeps_boost_on():
    # already on: need shrinks to 1.5 -> a 1.5 gap still boosts, 1.4 stops
    assert _dec(outdoor=23.5, indoor=25.0, on_now=True) is True
    assert _dec(outdoor=23.6, indoor=25.0, on_now=True) is False


def test_unknown_temps_never_boost():
    assert _dec(outdoor=None) is False
    assert _dec(indoor=None) is False


def test_quiet_hard_vetoes_boost():
    # thermally it WOULD boost (20 vs 25), but quiet wins
    assert _dec(outdoor=20.0, indoor=25.0, quiet=True) is False


# --- outdoor-cap hysteresis (2026-09-10: the cap was a hard edge) ------------
# Measured on switch.vmc_boost 8-9/9: an outdoor reading wobbling 23.9/24.2 gave
# 1, 10, 16 and 31 min cycles, because only the indoor margin had hysteresis.

def _cap(**kw):
    """Big indoor gap on purpose: the outdoor cap is the only gate under test."""
    return _dec(indoor=28.0, outdoor_hysteresis=0.5, **kw)


def test_cap_hysteresis_starts_below_the_cap_only():
    assert _cap(outdoor=23.9, on_now=False) is True
    assert _cap(outdoor=24.0, on_now=False) is False   # start needs < cap, as before


def test_cap_hysteresis_keeps_boost_on_through_the_wobble():
    assert _cap(outdoor=24.2, on_now=True) is True     # no longer flaps
    assert _cap(outdoor=24.49, on_now=True) is True
    assert _cap(outdoor=24.5, on_now=True) is False    # cap + hysteresis -> stop


def test_cap_hysteresis_defaults_to_the_old_hard_edge():
    # opt-in: callers that don't pass it keep the pre-v0.70.0 behaviour
    assert _dec(outdoor=24.2, indoor=28.0, on_now=True) is False


# --- duration cap / cooldown / min on-time (pure state machine) --------------
T0 = datetime(2026, 9, 10, 22, 0)
_MAX = timedelta(hours=4)
_COOL = timedelta(hours=1)
_MIN = timedelta(minutes=15)


def _step(**kw):
    base = dict(
        want=True, veto=False, on_now=False, now=T0,
        state=VmcBoostState(), max_on=_MAX, cooldown=_COOL, min_on=_MIN,
    )
    base.update(kw)
    return vmc_boost_step(**base)


def test_step_starts_and_stamps_the_start():
    state, on = _step()
    assert on is True
    assert state.on_since == T0 and state.cooldown_until is None


def test_step_holds_inside_the_cap():
    started, _ = _step()
    state, on = _step(on_now=True, state=started, now=T0 + _MAX - timedelta(minutes=1))
    assert on is True
    assert state.on_since == T0          # the start is NOT re-stamped every cycle


def test_step_caps_the_stint_and_owes_a_rest():
    started, _ = _step()
    state, on = _step(on_now=True, state=started, now=T0 + _MAX)
    assert on is False
    assert state.on_since is None
    assert state.cooldown_until == T0 + _MAX + _COOL


def test_step_cooldown_ignores_the_thermal_want():
    capped = VmcBoostState(cooldown_until=T0 + _COOL)
    state, on = _step(state=capped, now=T0 + _COOL - timedelta(minutes=1))
    assert on is False
    assert state.cooldown_until == capped.cooldown_until   # rest not shortened


def test_step_rearms_once_the_rest_is_served():
    capped = VmcBoostState(cooldown_until=T0 + _COOL)
    state, on = _step(state=capped, now=T0 + _COOL)
    assert on is True
    assert state.cooldown_until is None and state.on_since == T0 + _COOL


def test_step_rearm_without_want_just_clears():
    capped = VmcBoostState(cooldown_until=T0 + _COOL)
    state, on = _step(want=False, state=capped, now=T0 + _COOL)
    assert on is False and state == VmcBoostState()


def test_step_min_on_time_absorbs_a_marginal_flap():
    started, _ = _step()
    _, on = _step(want=False, on_now=True, state=started,
                  now=T0 + timedelta(minutes=5))
    assert on is True                                     # would have cycled before
    state, on = _step(want=False, on_now=True, state=started, now=T0 + _MIN)
    assert on is False and state == VmcBoostState()


def test_step_veto_beats_min_on_time():
    started, _ = _step()
    state, on = _step(veto=True, on_now=True, state=started,
                      now=T0 + timedelta(minutes=1))
    assert on is False                                    # never a loud fan over sleepers
    assert state.on_since is None


def test_step_veto_cannot_launder_an_owed_rest():
    capped = VmcBoostState(cooldown_until=T0 + _COOL)
    state, on = _step(veto=True, state=capped, now=T0 + timedelta(minutes=1))
    assert on is False and state.cooldown_until == capped.cooldown_until


def test_step_on_without_a_start_time_starts_the_clock():
    # a reload can leave us commanding ON with no bookkeeping: don't cap instantly
    state, on = _step(on_now=True, state=VmcBoostState(), now=T0)
    assert on is True and state.on_since == T0


# --- controller (edge-triggered, deploy-dark, release) -----------------------

async def _setup(hass):
    hass.states.async_set("climate.salotto_termostato_2", "cool",
                          {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_deploy_dark_master_off_no_writes(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    on = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set("switch.vmc_auto", "on")            # opt-in on
    hass.states.async_set(OUTDOOR_TEMP, "18")                 # cool outside
    coordinator.data = {"zone_temps": {"palestra": {"value": 26.0}}}

    await coordinator.vmc._evaluate()                          # master still OFF

    assert not [c for c in on if c.data.get("entity_id") in (GROUND, LIVING)]


async def test_boost_edge_and_release(hass):
    with freeze_time("2026-09-10 22:00:00") as frozen:
        entry = await _setup(hass)
        coordinator = entry.runtime_data
        vmc = coordinator.vmc
        # mock AFTER setup so the switch platform's real turn_on doesn't override it
        on = async_mock_service(hass, "switch", "turn_on")
        off = async_mock_service(hass, "switch", "turn_off")

        hass.states.async_set("switch.supervisor", "on")          # master on
        hass.states.async_set("switch.vmc_auto", "on")            # opt-in on
        hass.states.async_set(OUTDOOR_TEMP, "18")                 # cool outside
        coordinator.data = {"zone_temps": {
            "palestra": {"value": 26.0},   # ground group warm
            "kitchen": {"value": 26.0},    # living group warm
        }}

        await vmc._evaluate()
        assert any(c.data["entity_id"] == GROUND for c in on)
        assert any(c.data["entity_id"] == LIVING for c in on)

        # no NEW write on a second identical evaluate (edge-triggered, no re-assert)
        n_on = len(on)
        await vmc._evaluate()
        assert len(on) == n_on

        # outside warms above the room -> the thermal want drops, but MIN_ON
        # still owns the fan for its first quarter hour (anti-flap)
        hass.states.async_set(OUTDOOR_TEMP, "30")
        await vmc._evaluate()
        assert not off

        # past MIN_ON the same want releases what we set
        frozen.tick(VMC_BOOST_MIN_ON)
        await vmc._evaluate()
        assert any(c.data["entity_id"] == GROUND for c in off)
        assert any(c.data["entity_id"] == LIVING for c in off)


async def test_disable_releases(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    vmc = coordinator.vmc
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.vmc_auto", "on")
    hass.states.async_set(OUTDOOR_TEMP, "18")
    coordinator.data = {"zone_temps": {"palestra": {"value": 26.0}}}
    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in on)

    # turning the opt-in off -> the next evaluate hands the boost back
    hass.states.async_set("switch.vmc_auto", "off")
    await vmc._evaluate()
    assert any(c.data["entity_id"] == GROUND for c in off)


def _persons(hass, home: bool) -> None:
    for p in ("person.mattia_pontacolone", "person.ehi"):
        hass.states.async_set(p, "home" if home else "not_home")


async def _armed(hass):
    """Setup + master/opt-in on + cool outside + both groups warm."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    on = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.vmc_auto", "on")
    hass.states.async_set(OUTDOOR_TEMP, "18")
    coordinator.data = {"zone_temps": {
        "main_bedroom": {"value": 26.0},  # VMC 2 (bedroom-serving) warm
        "palestra": {"value": 26.0},      # VMC 1 (ground) warm
    }}
    return entry, coordinator.vmc, on


async def test_bedroom_unit_quiet_during_occupied_notte(hass):
    entry, vmc, on = await _armed(hass)
    hass.states.async_set("select.house_mode", "Notte")
    _persons(hass, home=True)                       # someone home

    await vmc._evaluate()

    assert any(c.data["entity_id"] == GROUND for c in on)      # ground still flushes
    assert not any(c.data["entity_id"] == LIVING for c in on)  # bedroom unit stays quiet


async def test_bedroom_unit_boosts_when_empty_at_night(hass):
    entry, vmc, on = await _armed(hass)
    hass.states.async_set("select.house_mode", "Notte")
    _persons(hass, home=False)                      # house free -> no quiet veto

    await vmc._evaluate()

    assert any(c.data["entity_id"] == LIVING for c in on)


async def test_bedroom_unit_boosts_daytime_occupied(hass):
    entry, vmc, on = await _armed(hass)
    hass.states.async_set("select.house_mode", "Casa")   # not Notte
    _persons(hass, home=True)

    await vmc._evaluate()

    assert any(c.data["entity_id"] == LIVING for c in on)  # quiet only during Notte



async def test_duration_cap_stops_the_boost_and_holds_the_cooldown(hass):
    """End-to-end: the cap writes OFF while it's still cool outside, and the
    thermal want alone can't re-arm it until the cooldown has elapsed."""
    with freeze_time("2026-09-10 22:00:00") as frozen:
        entry, vmc, on = await _armed(hass)
        off = async_mock_service(hass, "switch", "turn_off")
        hass.states.async_set("select.house_mode", "Casa")   # no quiet veto
        _persons(hass, home=True)

        await vmc._evaluate()
        assert any(c.data["entity_id"] == LIVING for c in on)

        frozen.tick(VMC_BOOST_MAX_ON)
        await vmc._evaluate()
        assert any(c.data["entity_id"] == LIVING for c in off)   # capped, still cool out

        n_on = len(on)
        frozen.tick(VMC_BOOST_COOLDOWN - timedelta(minutes=1))
        await vmc._evaluate()
        assert len(on) == n_on                                   # resting

        frozen.tick(timedelta(minutes=1))
        await vmc._evaluate()
        assert len(on) > n_on                                    # re-armed
