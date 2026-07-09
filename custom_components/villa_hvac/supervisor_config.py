"""Parsed-once options config (C3, F4c Phase 3).

`SupervisorConfig.from_options(entry.options)` coerces + clamps EVERY tunable
option ONCE per cycle into one frozen snapshot, replacing the ~30 scattered
`float(entry.options.get(...))`-with-try/except sites across the engine. Clamps
mirror the options-flow ranges so a stored out-of-range value is bounded here.

It lives at the `villa_hvac` level (it imports `const`) so the pure `supervisor/`
package stays import-pure — the planner reads a passed config by attribute
(duck-typed), never importing this type at runtime.

Enable SWITCHES (supervisor / duty_cycle / fan_pacing / pv_bias) + the HA-state
reads (house mode, setpoint, season, per-zone numbers) are NOT here — those are
measured/derived (controller.py), the clean other half of the split.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .const import (
    DEFAULT_BAND_SLAM,
    DEFAULT_BAND_WIDTH,
    DEFAULT_COMFORT_DAY_FROM,
    DEFAULT_COMFORT_DAY_TO,
    DEFAULT_COMFORT_ENABLED,
    DEFAULT_COMFORT_NIGHT_FROM,
    DEFAULT_COMFORT_NIGHT_TO,
    DEFAULT_COMFORT_RELAX,
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_COOLOFF,
    DEFAULT_DUTY_MAX_STINT,
    DEFAULT_DUTY_PEAK_OUTDOOR,
    DEFAULT_FREE_COOL_ENABLED,
    DEFAULT_FREE_COOL_OUTDOOR,
    DEFAULT_MIN_COMPRESSOR_OFF,
    DEFAULT_MIN_COMPRESSOR_ON,
    DEFAULT_MODEL_ENABLED,
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
    DEFAULT_REGIME_ENABLED,
    DEFAULT_RETURN_MARGIN_MIN,
    DEFAULT_SEFF_ENABLED,
    DEFAULT_RETURN_MAX_LEAD_HOURS,
    DEFAULT_REGIME_MEDIUM_RATIO,
    DEFAULT_REGIME_PEAK_RATIO,
    DEFAULT_SHADING_ENABLED,
    DEFAULT_SHADING_POSITION,
    DEFAULT_SHADING_PROPORTIONAL,
    DEFAULT_SHADING_SOLAR,
    DEFAULT_SOLAR_FORECAST,
    DEFAULT_SPLIT_CANTINA_SETPOINT,
    DEFAULT_SPLIT_MIN_OFF,
    DEFAULT_SPLIT_MIN_ON,
    DEFAULT_SPLIT_PALESTRA_SETPOINT,
    DEFAULT_SPLIT_RH_CEILING,
    DEFAULT_SPLIT_RH_FLOOR,
    OPT_BAND_SLAM,
    OPT_BAND_WIDTH,
    OPT_COMFORT_DAY_FROM,
    OPT_COMFORT_DAY_TO,
    OPT_COMFORT_ENABLED,
    OPT_COMFORT_NIGHT_FROM,
    OPT_COMFORT_NIGHT_TO,
    OPT_COMFORT_RELAX,
    OPT_DUTY_COMFORT_MAX,
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_DUTY_PEAK_OUTDOOR,
    OPT_FREE_COOL_ENABLED,
    OPT_FREE_COOL_OUTDOOR,
    OPT_MIN_COMPRESSOR_OFF,
    OPT_MIN_COMPRESSOR_ON,
    OPT_MODEL_ENABLED,
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
    OPT_REGIME_ENABLED,
    OPT_REGIME_MEDIUM_RATIO,
    OPT_REGIME_PEAK_RATIO,
    OPT_RETURN_MARGIN_MIN,
    OPT_RETURN_MAX_LEAD_HOURS,
    OPT_SEFF_ENABLED,
    SEFF_CONSUMERS_READY,
    OPT_SHADING_DEFAULT_POSITION,
    OPT_SHADING_ENABLED,
    OPT_SHADING_PROPORTIONAL,
    OPT_SHADING_SOLAR,
    OPT_SOLAR_FORECAST,
    OPT_SPLIT_CANTINA_SETPOINT,
    OPT_SPLIT_MIN_OFF,
    OPT_SPLIT_MIN_ON,
    OPT_SPLIT_PALESTRA_SETPOINT,
    OPT_SPLIT_RH_CEILING,
    OPT_SPLIT_RH_FLOOR,
    OPT_WEATHER_ENTITY,
    WEATHER_ENTITY_DEFAULT,
)


def _f(options, key, default, lo, hi):
    """Coerce an option to float, fall back to default, clamp to [lo, hi]."""
    try:
        val = float(options.get(key, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def _b(options, key, default):
    return bool(options.get(key, default))


@dataclass(frozen=True)
class SupervisorConfig:
    """Every option, parsed + clamped once. A validated per-cycle snapshot."""

    # #3 band
    band_width: float
    band_slam: float
    # #9 duty
    duty_max_stint: timedelta
    duty_cooloff: timedelta
    duty_comfort_max: float
    duty_peak_outdoor: float
    # #5 free-cool
    free_cool_enabled: bool
    free_cool_outdoor: float
    # #6 shading
    shading_enabled: bool
    shading_solar: float
    shading_default_position: int
    shading_proportional: bool
    # #9 pre-cool + forecast
    precool_offset: float
    precool_margin: float
    precool_max_depth: float
    lookahead: timedelta
    # F2 model
    model_learning_enabled: bool
    # F4a solar
    solar_forecast_enabled: bool
    # S_eff per-facade solar (STORY_SEFF; structurally dark until consumers ready)
    seff_enabled: bool
    # F4b comfort windows (raw HH:MM strings; parsed with the current clock)
    comfort_enabled: bool
    comfort_relax: float
    comfort_day_from: str
    comfort_day_to: str
    comfort_night_from: str
    comfort_night_to: str
    # F3 regime / coalescing
    regime_enabled: bool
    regime_peak_ratio: float
    regime_medium_ratio: float
    min_compressor_on: timedelta
    min_compressor_off: timedelta
    # PV bias
    pv_floor_rich: float
    pv_floor_poor: float
    pv_coast_relax: float
    pv_eff_fraction: float
    pv_eff_min: float
    pv_daily_need_kwh: float
    # #8 return-home (also feeds the planner's advisory arrival lead time)
    return_max_lead: timedelta
    return_margin: timedelta
    # #6 split-AC trio (cantina wine + palestra comfort setpoints, per-head dwell, RH band)
    split_cantina_setpoint: float
    split_palestra_setpoint: float
    split_min_on: timedelta
    split_min_off: timedelta
    split_rh_ceiling: float
    split_rh_floor: float
    # weather
    weather_entity: str

    @classmethod
    def from_options(cls, options) -> "SupervisorConfig":
        options = options or {}
        return cls(
            band_width=_f(options, OPT_BAND_WIDTH, DEFAULT_BAND_WIDTH, 0.4, 4.0),
            band_slam=_f(options, OPT_BAND_SLAM, DEFAULT_BAND_SLAM, 0.2, 3.0),
            duty_max_stint=timedelta(
                minutes=_f(options, OPT_DUTY_MAX_STINT, DEFAULT_DUTY_MAX_STINT, 15, 600)
            ),
            duty_cooloff=timedelta(
                minutes=_f(options, OPT_DUTY_COOLOFF, DEFAULT_DUTY_COOLOFF, 5, 240)
            ),
            duty_comfort_max=_f(
                options, OPT_DUTY_COMFORT_MAX, DEFAULT_DUTY_COMFORT_MAX, 22, 32
            ),
            duty_peak_outdoor=_f(
                options, OPT_DUTY_PEAK_OUTDOOR, DEFAULT_DUTY_PEAK_OUTDOOR, 24, 42
            ),
            free_cool_enabled=_b(options, OPT_FREE_COOL_ENABLED, DEFAULT_FREE_COOL_ENABLED),
            free_cool_outdoor=_f(
                options, OPT_FREE_COOL_OUTDOOR, DEFAULT_FREE_COOL_OUTDOOR, 10, 30
            ),
            shading_enabled=_b(options, OPT_SHADING_ENABLED, DEFAULT_SHADING_ENABLED),
            shading_solar=_f(options, OPT_SHADING_SOLAR, DEFAULT_SHADING_SOLAR, 50, 1000),
            shading_default_position=int(
                _f(options, OPT_SHADING_DEFAULT_POSITION, DEFAULT_SHADING_POSITION, 0, 100)
            ),
            shading_proportional=_b(
                options, OPT_SHADING_PROPORTIONAL, DEFAULT_SHADING_PROPORTIONAL
            ),
            precool_offset=_f(options, OPT_PRECOOL_OFFSET, DEFAULT_PRECOOL_OFFSET, 0, 5),
            precool_margin=_f(options, OPT_PRECOOL_MARGIN, DEFAULT_PRECOOL_MARGIN, 0, 10),
            precool_max_depth=_f(
                options, OPT_PRECOOL_MAX_DEPTH, DEFAULT_PRECOOL_MAX_DEPTH, 0, 6
            ),
            lookahead=timedelta(
                hours=_f(
                    options, OPT_PRECOOL_LOOKAHEAD_HOURS,
                    DEFAULT_PRECOOL_LOOKAHEAD_HOURS, 1, 24,
                )
            ),
            model_learning_enabled=_b(options, OPT_MODEL_ENABLED, DEFAULT_MODEL_ENABLED),
            solar_forecast_enabled=_b(options, OPT_SOLAR_FORECAST, DEFAULT_SOLAR_FORECAST),
            # ANDed with the code-level readiness constant: the option can never
            # light S_eff up while any §6 b-consumer still reads house GHI.
            seff_enabled=(
                _b(options, OPT_SEFF_ENABLED, DEFAULT_SEFF_ENABLED)
                and SEFF_CONSUMERS_READY
            ),
            comfort_enabled=_b(options, OPT_COMFORT_ENABLED, DEFAULT_COMFORT_ENABLED),
            comfort_relax=_f(options, OPT_COMFORT_RELAX, DEFAULT_COMFORT_RELAX, 0, 6),
            comfort_day_from=str(options.get(OPT_COMFORT_DAY_FROM, DEFAULT_COMFORT_DAY_FROM)),
            comfort_day_to=str(options.get(OPT_COMFORT_DAY_TO, DEFAULT_COMFORT_DAY_TO)),
            comfort_night_from=str(
                options.get(OPT_COMFORT_NIGHT_FROM, DEFAULT_COMFORT_NIGHT_FROM)
            ),
            comfort_night_to=str(
                options.get(OPT_COMFORT_NIGHT_TO, DEFAULT_COMFORT_NIGHT_TO)
            ),
            regime_enabled=_b(options, OPT_REGIME_ENABLED, DEFAULT_REGIME_ENABLED),
            regime_peak_ratio=_f(
                options, OPT_REGIME_PEAK_RATIO, DEFAULT_REGIME_PEAK_RATIO, 0.0, 5.0
            ),
            regime_medium_ratio=_f(
                options, OPT_REGIME_MEDIUM_RATIO, DEFAULT_REGIME_MEDIUM_RATIO, 0.0, 5.0
            ),
            min_compressor_on=timedelta(
                minutes=_f(options, OPT_MIN_COMPRESSOR_ON, DEFAULT_MIN_COMPRESSOR_ON, 0, 120)
            ),
            min_compressor_off=timedelta(
                minutes=_f(
                    options, OPT_MIN_COMPRESSOR_OFF, DEFAULT_MIN_COMPRESSOR_OFF, 0, 120
                )
            ),
            pv_floor_rich=_f(
                options, OPT_PV_BIAS_FLOOR_RICH, DEFAULT_PV_BIAS_FLOOR_RICH, 16, 28
            ),
            pv_floor_poor=_f(
                options, OPT_PV_BIAS_FLOOR_POOR, DEFAULT_PV_BIAS_FLOOR_POOR, 16, 28
            ),
            pv_coast_relax=_f(
                options, OPT_PV_BIAS_COAST_RELAX, DEFAULT_PV_BIAS_COAST_RELAX, 0, 5
            ),
            pv_eff_fraction=_f(
                options, OPT_PV_BIAS_EFF_FRACTION, DEFAULT_PV_BIAS_EFF_FRACTION, 0.1, 1.0
            ),
            pv_eff_min=_f(options, OPT_PV_BIAS_EFF_MIN, DEFAULT_PV_BIAS_EFF_MIN, 0.0, 1.0),
            pv_daily_need_kwh=_f(
                options, OPT_PV_BIAS_DAILY_NEED_KWH, DEFAULT_PV_BIAS_DAILY_NEED_KWH, 1, 200
            ),
            return_max_lead=timedelta(
                hours=_f(
                    options, OPT_RETURN_MAX_LEAD_HOURS, DEFAULT_RETURN_MAX_LEAD_HOURS,
                    0.5, 12,
                )
            ),
            return_margin=timedelta(
                minutes=_f(
                    options, OPT_RETURN_MARGIN_MIN, DEFAULT_RETURN_MARGIN_MIN, 0, 180
                )
            ),
            split_cantina_setpoint=_f(
                options, OPT_SPLIT_CANTINA_SETPOINT, DEFAULT_SPLIT_CANTINA_SETPOINT, 18, 26
            ),
            split_palestra_setpoint=_f(
                options, OPT_SPLIT_PALESTRA_SETPOINT, DEFAULT_SPLIT_PALESTRA_SETPOINT, 18, 28
            ),
            split_min_on=timedelta(
                minutes=_f(options, OPT_SPLIT_MIN_ON, DEFAULT_SPLIT_MIN_ON, 0, 60)
            ),
            split_min_off=timedelta(
                minutes=_f(options, OPT_SPLIT_MIN_OFF, DEFAULT_SPLIT_MIN_OFF, 0, 60)
            ),
            split_rh_ceiling=_f(
                options, OPT_SPLIT_RH_CEILING, DEFAULT_SPLIT_RH_CEILING, 40, 90
            ),
            split_rh_floor=_f(
                options, OPT_SPLIT_RH_FLOOR, DEFAULT_SPLIT_RH_FLOOR, 30, 80
            ),
            weather_entity=str(options.get(OPT_WEATHER_ENTITY) or WEATHER_ENTITY_DEFAULT),
        )
