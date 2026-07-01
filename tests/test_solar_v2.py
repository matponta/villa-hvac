"""Tests for the F4a-v2 nowcast-anchored solar curve (pure)."""
from __future__ import annotations

from custom_components.villa_hvac.supervisor import (
    solar_curve_v2,
    solar_forecast_curve,
    solar_nowcast_bias,
)

GHI = 1000.0
# A midday sun track (deg) with the forecast calling heavy cloud (0.6).
ELEV = [60.0, 55.0, 45.0, 30.0, 10.0]
CLOUD = [0.6, 0.6, 0.6, 0.6, 0.6]


# --- solar_nowcast_bias ------------------------------------------------------


def test_bias_none_actual_is_unity():
    assert solar_nowcast_bias(None, 400.0) == 1.0


def test_bias_below_horizon_model_is_unity():
    # sun not meaningfully up -> never scale (avoids wild ratios near the horizon)
    assert solar_nowcast_bias(500.0, 10.0) == 1.0


def test_bias_corrects_underprediction_clamped():
    # model 350, actual 1044 -> ratio ~2.98 clamped to hi=2.5
    assert solar_nowcast_bias(1044.0, 350.0) == 2.5


def test_bias_corrects_overprediction_clamped():
    assert solar_nowcast_bias(100.0, 800.0) == 0.4  # 0.125 clamped to lo


def test_bias_passthrough_when_forecast_right():
    # forecast matched reality -> ~1.0, no spurious correction
    b = solar_nowcast_bias(400.0, 410.0)
    assert 0.95 < b < 1.0


# --- solar_curve_v2 ----------------------------------------------------------


def test_curve_anchors_step0_to_live_reading():
    # base[0] = 1000*sin(60)*(1-0.6) ~= 346; anchor to 1044 -> bias clamps 2.5.
    base = solar_forecast_curve(elevations=ELEV, clouds=CLOUD, clear_sky_ghi=GHI)
    curve, anchored = solar_curve_v2(
        elevations=ELEV, clouds=CLOUD, clear_sky_ghi=GHI, actual_now=1044.0
    )
    assert anchored is True
    # whole curve scaled by the same (clamped) factor -> preserves shape
    assert curve[0] == round(base[0] * 2.5, 1)
    assert curve[2] == round(base[2] * 2.5, 1)


def test_curve_falls_back_when_no_reading():
    base = solar_forecast_curve(elevations=ELEV, clouds=CLOUD, clear_sky_ghi=GHI)
    curve, anchored = solar_curve_v2(
        elevations=ELEV, clouds=CLOUD, clear_sky_ghi=GHI, actual_now=None
    )
    assert anchored is False
    assert curve == base


def test_curve_ghi_constant_cancels_when_anchored():
    # With a live anchor, the clear_sky_ghi constant must not change the result
    # (curve = actual * shape[i]/shape[0]); different GHI -> same anchored curve.
    c1, _ = solar_curve_v2(
        elevations=ELEV, clouds=CLOUD, clear_sky_ghi=900.0, actual_now=500.0
    )
    c2, _ = solar_curve_v2(
        elevations=ELEV, clouds=CLOUD, clear_sky_ghi=1100.0, actual_now=500.0
    )
    # step 0 pinned to the reading regardless of GHI; forward within rounding.
    assert abs(c1[0] - c2[0]) <= 0.2
    assert abs(c1[2] - c2[2]) <= 0.5


def test_curve_empty_track():
    curve, anchored = solar_curve_v2(
        elevations=[], clouds=[], clear_sky_ghi=GHI, actual_now=500.0
    )
    assert curve == [] and anchored is False
