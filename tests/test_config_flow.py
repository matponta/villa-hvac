"""Config + options flow tests."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.data_entry_flow import FlowResultType

from custom_components.villa_hvac.const import (
    DOMAIN,
    OPT_AUTO_WAKE_TIME,
    OPT_AWAY_HOURS,
    OPT_NIGHT_THRESHOLD,
)


async def test_user_flow_creates_single_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Villa HVAC"


async def test_options_flow_saves_tunables(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_NIGHT_THRESHOLD: 25.0,
            OPT_AUTO_WAKE_TIME: "07:30:00",
            OPT_AWAY_HOURS: 24,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[OPT_NIGHT_THRESHOLD] == 25.0
    assert entry.options[OPT_AWAY_HOURS] == 24
