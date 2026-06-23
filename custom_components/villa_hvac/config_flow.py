"""Config + options flow for Villa HVAC (single instance)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    DEFAULT_AUTO_WAKE_TIME,
    DEFAULT_NIGHT_THRESHOLD,
    DOMAIN,
    OPT_AUTO_WAKE_TIME,
    OPT_NIGHT_THRESHOLD,
)


class VillaHvacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Villa HVAC."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single-instance setup; no fields for now."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Villa HVAC", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VillaHvacOptionsFlow()


class VillaHvacOptionsFlow(OptionsFlow):
    """Tunables for #2 (night heat-guard threshold + auto-wake time)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_NIGHT_THRESHOLD,
                    default=options.get(OPT_NIGHT_THRESHOLD, DEFAULT_NIGHT_THRESHOLD),
                ): vol.All(vol.Coerce(float), vol.Range(min=22, max=30)),
                vol.Optional(
                    OPT_AUTO_WAKE_TIME,
                    default=options.get(OPT_AUTO_WAKE_TIME, DEFAULT_AUTO_WAKE_TIME),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
