"""v0.84.0 — twin zones: Bagno Padronale 01/02 are one room (two thermostats,
one radiant valve 4/0/27). The follower is bound to the leader everywhere."""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import (
    DOMAIN,
    SEASON_REFERENCE_CLIMATE,
    ZONE_TWINS,
    ZONES,
    settings_zone,
)
from custom_components.villa_hvac.coordinator import _bind_twin_temps
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.policies import house_mode_policy
from custom_components.villa_hvac.supervisor import temperature_lever
from custom_components.villa_hvac.supervisor.winter_model import (
    WinterModel,
    WinterParams,
)

LEAD, FOLL = "bagno_padronale_01", "bagno_padronale_02"


def test_twin_map():
    assert ZONE_TWINS == {FOLL: LEAD}
    assert settings_zone(FOLL) == LEAD and settings_zone(LEAD) == LEAD


def test_twin_temperatures_are_the_mean():
    temps = {LEAD: {"value": 21.2, "source": "sensor"},
             FOLL: {"value": 21.5, "source": "sensor"}}
    _bind_twin_temps(temps)
    assert temps[LEAD]["value"] == temps[FOLL]["value"] == 21.4
    one = {LEAD: {"value": None}, FOLL: {"value": 20.0}}
    _bind_twin_temps(one)
    assert one[LEAD]["value"] == one[FOLL]["value"] == 20.0


def test_merge_twin_weights_by_samples():
    m = WinterModel()
    m.params = {
        LEAD: WinterParams(a=0.0116, k_h=0.388, lag_min=41, n_a=101, n_k=2, n_lag=2),
        FOLL: WinterParams(a=0.0101, k_h=0.300, lag_min=56.5, n_a=105, n_k=2, n_lag=2),
    }
    m.merge_twin(FOLL, LEAD)
    p = m.params[LEAD]
    assert FOLL not in m.params
    assert (p.n_a, p.n_k, p.n_lag) == (206, 4, 4)
    assert abs(p.k_h - 0.344) < 1e-9 and abs(p.lag_min - 48.75) < 1e-9
    m.merge_twin(FOLL, LEAD)                              # idempotent
    assert m.params[LEAD] == p


async def test_follower_is_bound_to_the_leader(hass):
    hass.states.async_set(SEASON_REFERENCE_CLIMATE, "heat", {"preset_mode": "comfort"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)

    def eid(domain, suffix):
        return reg.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{suffix}")

    for domain, suffix in (("switch", "{}_economy"), ("switch", "{}_pv_heat"),
                           ("switch", "{}_return_key"),
                           ("number", "setpoint_offset_{}")):
        assert eid(domain, suffix.format(LEAD)), suffix
        assert eid(domain, suffix.format(FOLL)) is None, suffix
    hass.states.async_set(eid("number", f"setpoint_offset_{LEAD}"), "1.0")
    hass.states.async_set(eid("switch", f"{LEAD}_economy"), "on")
    st = build_house_state(hass, entry, entry.runtime_data)
    assert st.zones[FOLL].setpoint_offset == st.zones[LEAD].setpoint_offset == -2.0
    assert {LEAD, FOLL} <= st.pv_heat_zones
    out = house_mode_policy(st)
    lead_t = out[temperature_lever(ZONES[LEAD]["climate"])]
    assert out[temperature_lever(ZONES[FOLL]["climate"])] == lead_t
