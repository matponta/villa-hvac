"""Pure wine-cellar core (v0.87.0, STORY_CELLAR.md).

The cantina vini keeps red wine for years. Its tools: the Daikin split head
(cool-side only, floor 18 °C, shares one outdoor unit with palestra + garage),
a circulation fan on a plug, and VMC 1's supply/extract. Two facts drive the
design, both measured live 2026-10-27/28:

- **Stagnant air lies.** With the fan OFF the EP sensor reads ~23.5 °C / 42 %
  (a warm pocket); within 10 min of the fan going ON it reads ~19.5 °C / 50 %,
  the same water content (~8.7 g/m³) — that is the cellar. So only a reading
  taken with the fan running for `mix_min` counts (`cellar_sample`); it is held
  between circulation runs and expires after `max_age`.
- **The split can only dry.** Cooling condenses water on the coil, so holding the
  wine cold and holding it humid pull against each other. Temperature wins
  (stability matters most for red wine); the setpoint is relaxed a little only
  while the air is too dry, and never above the season's ceiling.

Season windows (`CELLAR_WINDOWS`): the wine wants 12–14 °C and 60–70 % RH; the
unit cannot go below 18 °C. Summer = hold the floor (18), tolerate up to 20.
Winter = the cellar cools by itself; the split only trims above 18 and nothing
heats (a heat call would fight the shared outdoor unit) — below the low edge we
only warn. Humidity is the same year-round: dry above 70 %, relax below 55 %.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Status / reason strings (Italian: they surface as sensor states).
CELLAR_OK = "ok"
CELLAR_WARM = "calda"
CELLAR_COLD = "fredda"
CELLAR_DAMP = "umida"
CELLAR_DRY = "secca"
CELLAR_UNKNOWN = "sconosciuta"

REASON_HOLD = "mantieni"
REASON_DRYING = "deumidifica"
REASON_RELAXED = "aria_secca"
REASON_NO_SAMPLE = "senza_misura"


@dataclass(frozen=True)
class CellarWindow:
    """The target band for one season."""

    setpoint: float     # what the split holds (its own floor is 18)
    t_low: float        # below: too cold (warn only, nothing heats)
    t_high: float       # above: too warm
    rh_floor: float     # below: too dry -> relax the cooling
    rh_ceiling: float   # above: too damp -> dry
    relax: float        # °C the setpoint may rise while too dry (<= t_high)


@dataclass(frozen=True)
class CellarSample:
    """A reading of mixed cellar air (fan running long enough)."""

    temp: float
    rh: float
    at: datetime


@dataclass(frozen=True)
class CellarDecision:
    mode: str                # "cool" | "dry"
    setpoint: float | None   # None for dry
    drying: bool             # hysteresis state to carry
    relaxed: bool            # hysteresis state to carry
    reason: str


def cellar_window(
    summer: bool,
    *,
    summer_setpoint: float,
    winter_setpoint: float,
    rh_floor: float,
    rh_ceiling: float,
) -> CellarWindow:
    """The season's window (setpoints/RH band come from the options)."""
    if summer:
        return CellarWindow(
            setpoint=summer_setpoint, t_low=16.0, t_high=summer_setpoint + 2.0,
            rh_floor=rh_floor, rh_ceiling=rh_ceiling, relax=1.5,
        )
    return CellarWindow(
        setpoint=winter_setpoint, t_low=12.0, t_high=winter_setpoint + 1.0,
        rh_floor=rh_floor, rh_ceiling=rh_ceiling, relax=1.0,
    )


def cellar_sample(
    prev: CellarSample | None,
    *,
    fan_on: bool | None,
    fan_since: datetime | None,
    now: datetime,
    temp: float | None,
    rh: float | None,
    mix_min: timedelta,
) -> CellarSample | None:
    """Take the reading only once the fan has mixed the air; else keep `prev`."""
    if (
        fan_on and fan_since is not None and now - fan_since >= mix_min
        and temp is not None and rh is not None
    ):
        return CellarSample(temp, rh, now)
    return prev


def sample_fresh(
    sample: CellarSample | None, now: datetime, max_age: timedelta,
) -> CellarSample | None:
    if sample is None or now - sample.at > max_age:
        return None
    return sample


def cellar_fan_wanted(now: datetime, *, run_minutes: int, drying: bool) -> bool:
    """Circulate the first `run_minutes` of every hour, and all along while the
    split is drying (the damp air sits against the earth walls)."""
    return drying or now.minute < run_minutes


def cellar_head(
    window: CellarWindow,
    sample: CellarSample | None,
    *,
    drying: bool,
    relaxed: bool,
    hysteresis: float,
) -> CellarDecision:
    """Mode + setpoint for the wine head.

    - RH above the ceiling: dry. `dry` while the air is already at/below the
      setpoint (it barely cools), else `cool` (cools AND dries). Stays on until
      RH falls `hysteresis` below the ceiling.
    - RH below the floor: hold `cool`, setpoint raised by `relax` (capped at
      `t_high`) so the coil runs — and dries — less. Ends `hysteresis` above.
    - no mixed sample: plain `cool` at the setpoint (the head self-regulates on
      its own probe); the hysteresis states are dropped.
    """
    if sample is None:
        return CellarDecision("cool", window.setpoint, False, False, REASON_NO_SAMPLE)
    rh = sample.rh
    drying = rh > window.rh_ceiling or (drying and rh > window.rh_ceiling - hysteresis)
    relaxed = not drying and (
        rh < window.rh_floor or (relaxed and rh < window.rh_floor + hysteresis)
    )
    if drying:
        if sample.temp <= window.setpoint + 0.5:
            return CellarDecision("dry", None, True, False, REASON_DRYING)
        return CellarDecision("cool", window.setpoint, True, False, REASON_DRYING)
    if relaxed:
        sp = min(window.setpoint + window.relax, window.t_high)
        return CellarDecision("cool", sp, False, True, REASON_RELAXED)
    return CellarDecision("cool", window.setpoint, False, False, REASON_HOLD)


def cellar_status(window: CellarWindow, sample: CellarSample | None) -> str:
    """One word for the dashboard; temperature outranks humidity."""
    if sample is None:
        return CELLAR_UNKNOWN
    if sample.temp > window.t_high:
        return CELLAR_WARM
    if sample.temp < window.t_low:
        return CELLAR_COLD
    if sample.rh > window.rh_ceiling:
        return CELLAR_DAMP
    if sample.rh < window.rh_floor:
        return CELLAR_DRY
    return CELLAR_OK
