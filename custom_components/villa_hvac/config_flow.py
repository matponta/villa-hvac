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
    DEFAULT_AWAY_HOURS,
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_COOLOFF,
    DEFAULT_DUTY_MAX_STINT,
    DEFAULT_DUTY_PEAK_OUTDOOR,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_NIGHT_THRESHOLD,
    DEFAULT_PRECOOL_LEAD_HOURS,
    DEFAULT_PRECOOL_OFFSET,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SHADING_SOLAR,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_NIGHT,
    OPT_AUTO_WAKE_TIME,
    OPT_AWAY_HOURS,
    OPT_DUTY_COMFORT_MAX,
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_DUTY_PEAK_OUTDOOR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_NIGHT_THRESHOLD,
    OPT_PRECOOL_LEAD_HOURS,
    OPT_PRECOOL_OFFSET,
    OPT_SEASON,
    OPT_SHADING_ENABLED,
    OPT_SHADING_SOLAR,
    OPT_WEATHER_ENTITY,
    OPT_SUMMER_NOTTE_OFFSET,
    OPT_SUMMER_VIA_OFFSET,
    OPT_WINTER_NOTTE_OFFSET,
    OPT_WINTER_VIA_OFFSET,
    SEASON_AUTO,
    SEASON_OFFSET_DEFAULTS,
    SEASON_SUMMER,
    SEASON_WINTER,
    WEATHER_ENTITY_DEFAULT,
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
                vol.Optional(
                    OPT_AWAY_HOURS,
                    default=options.get(OPT_AWAY_HOURS, DEFAULT_AWAY_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
                vol.Optional(
                    OPT_SEASON,
                    default=options.get(OPT_SEASON, SEASON_AUTO),
                ): vol.In([SEASON_AUTO, SEASON_SUMMER, SEASON_WINTER]),
                vol.Optional(
                    OPT_SUMMER_VIA_OFFSET,
                    default=options.get(
                        OPT_SUMMER_VIA_OFFSET,
                        SEASON_OFFSET_DEFAULTS[SEASON_SUMMER][HOUSE_MODE_AWAY],
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=-10, max=10)),
                vol.Optional(
                    OPT_SUMMER_NOTTE_OFFSET,
                    default=options.get(
                        OPT_SUMMER_NOTTE_OFFSET,
                        SEASON_OFFSET_DEFAULTS[SEASON_SUMMER][HOUSE_MODE_NIGHT],
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=-10, max=10)),
                vol.Optional(
                    OPT_WINTER_VIA_OFFSET,
                    default=options.get(
                        OPT_WINTER_VIA_OFFSET,
                        SEASON_OFFSET_DEFAULTS[SEASON_WINTER][HOUSE_MODE_AWAY],
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=-10, max=10)),
                vol.Optional(
                    OPT_WINTER_NOTTE_OFFSET,
                    default=options.get(
                        OPT_WINTER_NOTTE_OFFSET,
                        SEASON_OFFSET_DEFAULTS[SEASON_WINTER][HOUSE_MODE_NIGHT],
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=-10, max=10)),
                vol.Optional(
                    OPT_FREE_COOL_ENABLED,
                    default=options.get(
                        OPT_FREE_COOL_ENABLED, DEFAULT_FREE_COOL_ENABLED
                    ),
                ): bool,
                vol.Optional(
                    OPT_FREE_COOL_OUTDOOR,
                    default=options.get(
                        OPT_FREE_COOL_OUTDOOR, DEFAULT_FREE_COOL_OUTDOOR
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=10, max=30)),
                vol.Optional(
                    OPT_SHADING_ENABLED,
                    default=options.get(OPT_SHADING_ENABLED, DEFAULT_SHADING_ENABLED),
                ): bool,
                vol.Optional(
                    OPT_SHADING_SOLAR,
                    default=options.get(OPT_SHADING_SOLAR, DEFAULT_SHADING_SOLAR),
                ): vol.All(vol.Coerce(float), vol.Range(min=50, max=1000)),
                vol.Optional(
                    OPT_DUTY_MAX_STINT,
                    default=options.get(OPT_DUTY_MAX_STINT, DEFAULT_DUTY_MAX_STINT),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=600)),
                vol.Optional(
                    OPT_DUTY_COOLOFF,
                    default=options.get(OPT_DUTY_COOLOFF, DEFAULT_DUTY_COOLOFF),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=240)),
                vol.Optional(
                    OPT_DUTY_COMFORT_MAX,
                    default=options.get(OPT_DUTY_COMFORT_MAX, DEFAULT_DUTY_COMFORT_MAX),
                ): vol.All(vol.Coerce(float), vol.Range(min=22, max=32)),
                vol.Optional(
                    OPT_DUTY_PEAK_OUTDOOR,
                    default=options.get(
                        OPT_DUTY_PEAK_OUTDOOR, DEFAULT_DUTY_PEAK_OUTDOOR
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=24, max=42)),
                vol.Optional(
                    OPT_WEATHER_ENTITY,
                    default=options.get(OPT_WEATHER_ENTITY, WEATHER_ENTITY_DEFAULT),
                ): str,
                vol.Optional(
                    OPT_PRECOOL_LEAD_HOURS,
                    default=options.get(
                        OPT_PRECOOL_LEAD_HOURS, DEFAULT_PRECOOL_LEAD_HOURS
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                vol.Optional(
                    OPT_PRECOOL_OFFSET,
                    default=options.get(OPT_PRECOOL_OFFSET, DEFAULT_PRECOOL_OFFSET),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
