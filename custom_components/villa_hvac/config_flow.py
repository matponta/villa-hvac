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
    DEFAULT_BAND_SLAM,
    DEFAULT_BAND_WIDTH,
    DEFAULT_COMFORT_DAY_FROM,
    DEFAULT_COMFORT_DAY_TO,
    DEFAULT_COMFORT_ENABLED,
    DEFAULT_COMFORT_NIGHT_FROM,
    DEFAULT_COMFORT_FLOOR,
    DEFAULT_COMFORT_NIGHT_TO,
    DEFAULT_COMFORT_RELAX,
    DEFAULT_FAN_MIN,
    DEFAULT_MODEL_ENABLED,
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_COOLOFF,
    DEFAULT_DUTY_MAX_STINT,
    DEFAULT_DUTY_PEAK_OUTDOOR,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_NIGHT_THRESHOLD,
    DEFAULT_PRECOOL_LOOKAHEAD_HOURS,
    DEFAULT_PRECOOL_MARGIN,
    DEFAULT_PRECOOL_MAX_DEPTH,
    DEFAULT_PRECOOL_OFFSET,
    DEFAULT_PV_BIAS_COAST_RELAX,
    DEFAULT_PV_BIAS_DAILY_NEED_KWH,
    DEFAULT_PV_BIAS_EFF_FRACTION,
    DEFAULT_PV_BIAS_EFF_MIN,
    DEFAULT_PV_BIAS_FLOOR_POOR,
    DEFAULT_PV_BIAS_FLOOR_RICH,
    DEFAULT_RETURN_MARGIN_MIN,
    DEFAULT_RETURN_MAX_LEAD_HOURS,
    DEFAULT_SEFF_ENABLED,
    DEFAULT_SOLAR_FORECAST,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SHADING_POSITION,
    DEFAULT_SHADING_PROPORTIONAL,
    DEFAULT_SHADING_SOLAR,
    DOMAIN,
    HOUSE_MODE_AWAY,
    HOUSE_MODE_NIGHT,
    OPT_AUTO_WAKE_TIME,
    OPT_AWAY_HOURS,
    OPT_BAND_SLAM,
    OPT_BAND_WIDTH,
    OPT_COMFORT_DAY_FROM,
    OPT_COMFORT_DAY_TO,
    OPT_COMFORT_ENABLED,
    OPT_COMFORT_FLOOR,
    OPT_COMFORT_NIGHT_FROM,
    OPT_COMFORT_NIGHT_TO,
    OPT_COMFORT_RELAX,
    OPT_FAN_MIN,
    OPT_DUTY_COMFORT_MAX,
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_DUTY_PEAK_OUTDOOR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_MODEL_ENABLED,
    OPT_NIGHT_THRESHOLD,
    OPT_NOTIFY_TARGET,
    OPT_PRECOOL_LOOKAHEAD_HOURS,
    OPT_PRECOOL_MARGIN,
    OPT_PRECOOL_MAX_DEPTH,
    OPT_PRECOOL_OFFSET,
    OPT_PV_BIAS_COAST_RELAX,
    OPT_PV_BIAS_DAILY_NEED_KWH,
    OPT_PV_BIAS_EFF_FRACTION,
    OPT_PV_BIAS_EFF_MIN,
    OPT_PV_BIAS_FLOOR_POOR,
    OPT_PV_BIAS_FLOOR_RICH,
    OPT_RETURN_MARGIN_MIN,
    OPT_RETURN_MAX_LEAD_HOURS,
    OPT_SEASON,
    OPT_SEFF_ENABLED,
    OPT_SHADING_DEFAULT_POSITION,
    OPT_SHADING_ENABLED,
    OPT_SHADING_PROPORTIONAL,
    OPT_SHADING_SOLAR,
    OPT_SOLAR_FORECAST,
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
                    OPT_SHADING_DEFAULT_POSITION,
                    default=options.get(
                        OPT_SHADING_DEFAULT_POSITION, DEFAULT_SHADING_POSITION
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Optional(
                    OPT_SHADING_PROPORTIONAL,
                    default=options.get(
                        OPT_SHADING_PROPORTIONAL, DEFAULT_SHADING_PROPORTIONAL
                    ),
                ): bool,
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
                # F4c comfort FLOOR: the lower bound on the band center (symmetric to
                # duty_comfort_max). Default 22 (= the default setpoint 24 − 2); until
                # pinned here it tracks house_setpoint − 2 dynamically.
                vol.Optional(
                    OPT_COMFORT_FLOOR,
                    default=options.get(OPT_COMFORT_FLOOR, DEFAULT_COMFORT_FLOOR),
                ): vol.All(vol.Coerce(float), vol.Range(min=16, max=26)),
                vol.Optional(
                    OPT_WEATHER_ENTITY,
                    default=options.get(OPT_WEATHER_ENTITY, WEATHER_ENTITY_DEFAULT),
                ): str,
                vol.Optional(
                    OPT_PRECOOL_LOOKAHEAD_HOURS,
                    default=options.get(
                        OPT_PRECOOL_LOOKAHEAD_HOURS, DEFAULT_PRECOOL_LOOKAHEAD_HOURS
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
                vol.Optional(
                    OPT_PRECOOL_MARGIN,
                    default=options.get(OPT_PRECOOL_MARGIN, DEFAULT_PRECOOL_MARGIN),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional(
                    OPT_PRECOOL_OFFSET,
                    default=options.get(OPT_PRECOOL_OFFSET, DEFAULT_PRECOOL_OFFSET),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                vol.Optional(
                    OPT_BAND_WIDTH,
                    default=options.get(OPT_BAND_WIDTH, DEFAULT_BAND_WIDTH),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.4, max=4)),
                vol.Optional(
                    OPT_BAND_SLAM,
                    default=options.get(OPT_BAND_SLAM, DEFAULT_BAND_SLAM),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=3)),
                vol.Optional(
                    OPT_FAN_MIN,
                    default=options.get(OPT_FAN_MIN, DEFAULT_FAN_MIN),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Optional(
                    OPT_MODEL_ENABLED,
                    default=options.get(OPT_MODEL_ENABLED, DEFAULT_MODEL_ENABLED),
                ): bool,
                vol.Optional(
                    OPT_PRECOOL_MAX_DEPTH,
                    default=options.get(
                        OPT_PRECOOL_MAX_DEPTH, DEFAULT_PRECOOL_MAX_DEPTH
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=6)),
                vol.Optional(
                    OPT_SOLAR_FORECAST,
                    default=options.get(OPT_SOLAR_FORECAST, DEFAULT_SOLAR_FORECAST),
                ): bool,
                vol.Optional(
                    OPT_SEFF_ENABLED,
                    default=options.get(OPT_SEFF_ENABLED, DEFAULT_SEFF_ENABLED),
                ): bool,
                vol.Optional(
                    OPT_COMFORT_ENABLED,
                    default=options.get(OPT_COMFORT_ENABLED, DEFAULT_COMFORT_ENABLED),
                ): bool,
                vol.Optional(
                    OPT_COMFORT_RELAX,
                    default=options.get(OPT_COMFORT_RELAX, DEFAULT_COMFORT_RELAX),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=6)),
                vol.Optional(
                    OPT_COMFORT_DAY_FROM,
                    default=options.get(OPT_COMFORT_DAY_FROM, DEFAULT_COMFORT_DAY_FROM),
                ): str,
                vol.Optional(
                    OPT_COMFORT_DAY_TO,
                    default=options.get(OPT_COMFORT_DAY_TO, DEFAULT_COMFORT_DAY_TO),
                ): str,
                vol.Optional(
                    OPT_COMFORT_NIGHT_FROM,
                    default=options.get(
                        OPT_COMFORT_NIGHT_FROM, DEFAULT_COMFORT_NIGHT_FROM
                    ),
                ): str,
                vol.Optional(
                    OPT_COMFORT_NIGHT_TO,
                    default=options.get(OPT_COMFORT_NIGHT_TO, DEFAULT_COMFORT_NIGHT_TO),
                ): str,
                # #8 return-home pre-conditioning.
                vol.Optional(
                    OPT_RETURN_MAX_LEAD_HOURS,
                    default=options.get(
                        OPT_RETURN_MAX_LEAD_HOURS, DEFAULT_RETURN_MAX_LEAD_HOURS
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=12)),
                vol.Optional(
                    OPT_RETURN_MARGIN_MIN,
                    default=options.get(
                        OPT_RETURN_MARGIN_MIN, DEFAULT_RETURN_MARGIN_MIN
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=180)),
                vol.Optional(
                    OPT_NOTIFY_TARGET,
                    default=options.get(OPT_NOTIFY_TARGET, ""),
                ): str,
                # PV/energy-aware daily pre-cool (F4c-lite). Floors are SUMMER
                # cooling values — revise for the heating season.
                vol.Optional(
                    OPT_PV_BIAS_FLOOR_RICH,
                    default=options.get(
                        OPT_PV_BIAS_FLOOR_RICH, DEFAULT_PV_BIAS_FLOOR_RICH
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=16, max=28)),
                vol.Optional(
                    OPT_PV_BIAS_FLOOR_POOR,
                    default=options.get(
                        OPT_PV_BIAS_FLOOR_POOR, DEFAULT_PV_BIAS_FLOOR_POOR
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=16, max=28)),
                vol.Optional(
                    OPT_PV_BIAS_COAST_RELAX,
                    default=options.get(
                        OPT_PV_BIAS_COAST_RELAX, DEFAULT_PV_BIAS_COAST_RELAX
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                vol.Optional(
                    OPT_PV_BIAS_EFF_FRACTION,
                    default=options.get(
                        OPT_PV_BIAS_EFF_FRACTION, DEFAULT_PV_BIAS_EFF_FRACTION
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1.0)),
                vol.Optional(
                    OPT_PV_BIAS_EFF_MIN,
                    default=options.get(
                        OPT_PV_BIAS_EFF_MIN, DEFAULT_PV_BIAS_EFF_MIN
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1.0)),
                vol.Optional(
                    OPT_PV_BIAS_DAILY_NEED_KWH,
                    default=options.get(
                        OPT_PV_BIAS_DAILY_NEED_KWH, DEFAULT_PV_BIAS_DAILY_NEED_KWH
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=1, max=200)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
