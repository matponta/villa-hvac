"""C3: SupervisorConfig.from_options coerces + clamps every option once."""
from __future__ import annotations

from datetime import timedelta

from custom_components.villa_hvac.const import (
    DEFAULT_BAND_WIDTH,
    DEFAULT_DUTY_COMFORT_MAX,
    DEFAULT_DUTY_MAX_STINT,
    WEATHER_ENTITY_DEFAULT,
)
from custom_components.villa_hvac.supervisor_config import SupervisorConfig


def test_defaults_when_empty():
    cfg = SupervisorConfig.from_options({})
    assert cfg.band_width == DEFAULT_BAND_WIDTH
    assert cfg.duty_comfort_max == DEFAULT_DUTY_COMFORT_MAX
    assert cfg.duty_max_stint == timedelta(minutes=DEFAULT_DUTY_MAX_STINT)
    assert cfg.weather_entity == WEATHER_ENTITY_DEFAULT
    assert cfg.regime_enabled is False


def test_clamps_out_of_range():
    cfg = SupervisorConfig.from_options(
        {"band_width": 99, "duty_max_stint_min": 0, "duty_comfort_max": 5}
    )
    assert cfg.band_width == 4.0                       # clamped to the max
    assert cfg.duty_max_stint == timedelta(minutes=15)  # clamped to the min
    assert cfg.duty_comfort_max == 22.0                 # clamped to the min


def test_garbage_coerces_to_default():
    cfg = SupervisorConfig.from_options({"band_width": "not-a-number"})
    assert cfg.band_width == DEFAULT_BAND_WIDTH


def test_none_options_is_safe():
    cfg = SupervisorConfig.from_options(None)
    assert cfg.band_width == DEFAULT_BAND_WIDTH


def test_frozen_snapshot():
    cfg = SupervisorConfig.from_options({})
    import dataclasses
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.band_width = 1.0  # type: ignore[misc]
