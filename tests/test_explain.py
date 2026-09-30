"""v0.75.0 per-room explain surface."""
from __future__ import annotations

from datetime import datetime, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.supervisor import (
    HouseState,
    ZoneSnapshot,
    fan_lever,
    preset_lever,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor.explain import LiveClimate, explain_room
from custom_components.villa_hvac.supervisor.telemetry import UnitTelemetry
from custom_components.villa_hvac.supervisor.thermal import ModelValidity

from .helpers import enable_supervisor, seed_thermostats

T0 = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
CL = "climate.studio_termostato_2"
FAN = "fan.fancoil_studio_pianerottolo_p1"
TL, PL, FL = temperature_lever(CL), preset_lever(CL), fan_lever(FAN)


def _z(**kw):
    base = dict(zone_id="office", name="Studio", climate=CL, emitter="fancoil",
                fancoil_units=((FAN, "switch.m"),), temp=25.5, demand=True)
    base.update(kw)
    return ZoneSnapshot(**base)


def _explain(z=None, *, desired=None, owners=None, decisions=None, fan_owner="knx_auto",
             state=None, **kw):
    z = z or _z()
    state = state or HouseState(now=T0, zones={z.zone_id: z}, house_mode="Casa",
                                season="summer")
    return explain_room(
        z, state, live=LiveClimate(setpoint=24.0, preset="comfort"),
        desired=desired or {}, owners=owners or {}, decisions=decisions or {},
        fan_owner=fan_owner, fan_note=None, **kw,
    )


def test_state_priority_ladder():
    assert _explain(enabled=False).state == "nativa"
    assert _explain(_z(enabled=False)).state == "disabilitata"
    assert _explain(_z(paused=True)).state == "finestre_aperte"
    assert _explain(
        desired={TL: 24.0}, owners={TL: "house_mode_policy"},
        decisions={TL: {"note": "override"}},
    ).state == "manuale"
    assert _explain(desired={TL: 24.0}, owners={TL: "house_mode_policy"}).state == (
        "supervisionata"
    )
    assert _explain(
        desired={TL: 26.0, PL: "comfort"},
        owners={TL: "DemandShedController", PL: "house_mode_policy"},
    ).state == "riposo_pdc"
    assert _explain(
        desired={FL: 70}, owners={FL: "P1GuardController"},
        fan_owner="P1GuardController",
    ).state == "guardia"
    assert _explain().state == "nativa"


def test_sentence_carries_why_valve_fan_and_model():
    tel = UnitTelemetry(zone_id="office", valve_duty=0.35, valve_strokes=4,
                        valve_open=False, fan_delivered=100, manuale_on=False,
                        observed_s=3600)
    bad = ModelValidity(abc_valid=False, k_valid=False, skill=0.02,
                        reasons=("low_skill",))
    e = _explain(
        desired={TL: 26.0}, owners={TL: "DemandShedController"},
        telemetry=tel, validity=bad,
        shed_reason="unica stanza a tenere acceso il PdC, già entro il comfort",
    )
    s = e.sentence
    assert s.startswith("Studio: 25.5 °C; termostato a 24.0 °C (obiettivo 26.0 °C)")
    assert "riposo PdC: unica stanza" in s
    assert "valvola chiusa, aperta 35% nell'ultima ora (4 aperture)" in s
    assert "ventola 100% in AUTO KNX" in s
    assert "modello non affidabile (low_skill)" in s
    assert e.attributes["setpoint_owner"] == "DemandShedController"
    assert e.attributes["valve_duty_1h"] == 35
    assert e.attributes["model_valid"] is False


async def test_explain_sensor_is_live_and_deploy_dark_aware(hass):
    seed_thermostats(hass, temperature=24.0)
    hass.states.async_set("sensor.clima_studio", "25.5")
    hass.states.async_set("binary_sensor.fancoil_studio_pianerottolo_p1_valvola", "on")
    hass.states.async_set(FAN, "on", {"percentage": 100})
    hass.states.async_set("switch.fancoil_studio_pianerottolo_p1_manuale", "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await coordinator.engine._tick()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    st = hass.states.get("sensor.office_studio_stato_hvac")
    assert st is not None and st.state == "nativa"          # master off
    assert "supervisore spento" in st.attributes["explanation"]

    await enable_supervisor(hass)
    await coordinator.engine._tick()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    st = hass.states.get("sensor.office_studio_stato_hvac")
    assert st.state == "supervisionata"                      # house_mode owns it
    assert st.attributes["setpoint_owner"] == "house_mode_policy"
    assert "valvola APERTA" in st.attributes["explanation"]
