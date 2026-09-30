"""v0.74.0 mass-aware summer control: Via mass maintenance + demand shedding."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor.mass import (
    SHED_MAX_HOLD,
    SHED_MIN_RUN,
    SHED_REARM,
    DemandShedController,
    apply_mass_maintenance,
    mass_via_offset,
)

from .helpers import enable_supervisor, seed_thermostats

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


# --- mass maintenance -----------------------------------------------------------

def test_via_banks_below_the_peak_and_coasts_at_it():
    assert mass_via_offset(mode_offset=5.0, outdoor=26.0, peak=30.0, mild=2.0,
                           coasting=False) == (2.0, False)
    assert mass_via_offset(mode_offset=5.0, outdoor=31.0, peak=30.0, mild=2.0,
                           coasting=False) == (5.0, True)
    # hysteresis: stays coasting until 1 °C below the peak
    assert mass_via_offset(mode_offset=5.0, outdoor=29.5, peak=30.0, mild=2.0,
                           coasting=True) == (5.0, True)
    assert mass_via_offset(mode_offset=5.0, outdoor=28.9, peak=30.0, mild=2.0,
                           coasting=True) == (2.0, False)


def test_unknown_outdoor_keeps_the_phase_and_never_deepens_a_mild_mode():
    assert mass_via_offset(mode_offset=5.0, outdoor=None, peak=30.0, mild=2.0,
                           coasting=True) == (5.0, True)
    # a Via offset already milder than the cap is never raised
    assert mass_via_offset(mode_offset=1.0, outdoor=20.0, peak=30.0, mild=2.0,
                           coasting=False) == (1.0, False)


def test_apply_only_in_summer_via_with_the_switch_on():
    st = HouseState(now=T0, house_mode="Via", mode_offset=5.0, season="summer",
                    outdoor_temp=25.0, mass_maintenance_enabled=True)
    out, coasting, phase = apply_mass_maintenance(st, False, mild=2.0, peak=30.0)
    assert out.mode_offset == 2.0 and phase == "bank" and out.mass_phase == "bank"
    for other in (
        replace(st, mass_maintenance_enabled=False),
        replace(st, house_mode="Vacanza", mode_offset=None),
        replace(st, house_mode="Notte"),
        replace(st, season="winter"),
    ):
        out, _, phase = apply_mass_maintenance(other, False, mild=2.0, peak=30.0)
        assert out is other and phase is None


# --- demand shedding --------------------------------------------------------------

def _zone(zid, *, temp, demand, climate=True, **kw):
    return ZoneSnapshot(
        zone_id=zid, name=zid, climate=f"climate.{zid}" if climate else None,
        emitter="fancoil", fancoil_units=((f"fan.{zid}", f"switch.{zid}_m"),),
        temp=temp, demand=demand, **kw,
    )


def _state(now, zones, *, consenso="on", enabled=True, **kw):
    base = dict(
        now=now, zones={z.zone_id: z for z in zones}, season="summer",
        house_setpoint=24.0, mode_offset=0.0, duty_comfort_max=27.0,
        consenso_freddo=consenso, blocco="off", demand_shedding_enabled=enabled,
        config_shed_max_callers=1,
    )
    base.update(kw)
    return HouseState(**base)


def _sept9(now, *, office=25.5, office_demand=True, bedroom_demand=False):
    """9/9: every room flat at setpoint, the office alone still calling."""
    return [
        _zone("office", temp=office, demand=office_demand),
        _zone("main_bedroom", temp=24.4, demand=bedroom_demand),
        _zone("living_room", temp=24.3, demand=False),
    ]


def test_sheds_the_lone_in_comfort_caller_after_min_run():
    ctl = DemandShedController()
    assert ctl(_state(T0, _sept9(T0))) == {}                     # stint starts
    assert ctl(_state(T0 + SHED_MIN_RUN - timedelta(seconds=30), _sept9(T0))) == {}
    out = ctl(_state(T0 + SHED_MIN_RUN, _sept9(T0)))
    assert out == {temperature_lever("climate.office"): 26.0}   # 25.5 + 0.5
    # holds while the valve closes and the compressor stops (no short cycle)
    t = T0 + SHED_MIN_RUN + timedelta(minutes=5)
    out = ctl(_state(t, _sept9(t, office=25.7, office_demand=False), consenso="off"))
    assert out == {temperature_lever("climate.office"): 26.2}


def test_releases_when_another_room_calls_then_rearm_cooldown():
    ctl = DemandShedController()
    ctl(_state(T0, _sept9(T0)))
    ctl(_state(T0 + SHED_MIN_RUN, _sept9(T0)))
    t = T0 + SHED_MIN_RUN + timedelta(minutes=12)
    out = ctl(_state(t, _sept9(t, office=25.9, office_demand=False,
                               bedroom_demand=True)))
    assert out == {}                                             # rides along
    assert "chiama" in ctl.state.last_reason["office"]
    # the bedroom is satisfied again, office alone: cooldown blocks a re-shed
    t2 = t + SHED_MIN_RUN
    ctl(_state(t, _sept9(t)))
    assert ctl(_state(t2, _sept9(t2))) == {}
    t3 = t + SHED_REARM + SHED_MIN_RUN
    assert ctl(_state(t3, _sept9(t3))) != {}


def test_releases_at_the_comfort_ceiling_and_after_max_hold():
    ctl = DemandShedController()
    ctl(_state(T0, _sept9(T0)))
    ctl(_state(T0 + SHED_MIN_RUN, _sept9(T0)))
    t = T0 + SHED_MIN_RUN + timedelta(minutes=30)
    assert ctl(_state(t, _sept9(t, office=27.0, office_demand=False),
                      consenso="off")) == {}
    ctl2 = DemandShedController()
    ctl2(_state(T0, _sept9(T0)))
    ctl2(_state(T0 + SHED_MIN_RUN, _sept9(T0)))
    t = T0 + SHED_MIN_RUN + SHED_MAX_HOLD
    assert ctl2(_state(t, _sept9(t, office=26.0, office_demand=False),
                       consenso="off")) == {}


def test_no_shed_when_too_warm_too_many_callers_or_not_ours():
    for zones in (
        _sept9(T0, office=26.8),                                   # near the ceiling
        _sept9(T0, bedroom_demand=True),                           # two callers
        [_zone("office", temp=25.5, demand=True, manuale_on=True)],  # guard owns it
        [_zone("office", temp=25.5, demand=True, paused=True)],
        [_zone("rack", temp=30.0, demand=True, climate=False)],    # hardware
        [_zone("main_bedroom", temp=25.0, demand=True, bedroom=True)],
    ):
        ctl = DemandShedController()
        night = any(z.bedroom for z in zones)
        ctl(_state(T0, zones, night_active=night))
        assert ctl(_state(T0 + SHED_MIN_RUN, zones, night_active=night)) == {}


def test_disabled_switch_or_winter_releases_everything():
    ctl = DemandShedController()
    ctl(_state(T0, _sept9(T0)))
    assert ctl(_state(T0 + SHED_MIN_RUN, _sept9(T0))) != {}
    t = T0 + SHED_MIN_RUN + timedelta(minutes=1)
    assert ctl(_state(t, _sept9(t), enabled=False)) == {}
    assert ctl.state.shed_since == {}


def test_open_space_follower_valve_counts_for_its_leader():
    zones = [
        _zone("living_room", temp=25.0, demand=False),
        ZoneSnapshot(zone_id="kitchen", name="k", climate=None, emitter="fancoil",
                     fancoil_units=(("fan.k", "switch.k_m"),), follows="living_room",
                     temp=25.0, demand=True),
    ]
    ctl = DemandShedController()
    ctl(_state(T0, zones))
    out = ctl(_state(T0 + SHED_MIN_RUN, zones))
    assert out == {temperature_lever("climate.living_room"): 25.5}


# --- engine: Via writes the mild setback --------------------------------------------

async def test_engine_via_writes_mild_offset_with_mass_maintenance(hass):
    seed_thermostats(hass, temperature=24.0)
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "25.0")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await enable_supervisor(hass)
    for svc in ("set_preset_mode",):
        async_mock_service(hass, "climate", svc)
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "switch", "turn_off")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.mass_maintenance"}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.house_mode", "option": "Via"}, blocking=True,
    )
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    await engine._run()
    written = {c.data["temperature"] for c in temps}
    assert written and written <= {26.0}          # 24 + 2, not 24 + 5
