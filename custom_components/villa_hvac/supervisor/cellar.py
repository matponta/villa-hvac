"""Pure wine-cellar core (v0.87.0, STORY_CELLAR.md).

The cantina vini keeps red wine for years. Its tools: the Daikin split head
(cool-side only, floor 18 °C, shares one outdoor unit with palestra + garage),
a circulation fan on a plug, and VMC 1's supply/extract. Two facts drive the
design, both measured live 2026-10-27/28:

- **Stagnant air lies.** With the fan OFF the EP sensor reads ~23.5 °C / 42 %
  (a warm pocket); within 10 min of the fan going ON it reads ~19.5 °C / 50 %,
  the same water content (~8.7 g/m³) — that is the cellar. So only a reading
  taken with the fan running for `mix_min` counts (`cellar_sample`); it is held
  between circulation runs and expires after `max_age`. v0.91.0: within one run
  the reading still swings ±1.5 °C in minutes (the head's cold jet passing the
  sensor: 19.7 ↔ 23 °C on 31/10), so the sample is the MEAN of the run's mixed
  readings, not its last value — the last value alone read "calda" ~45 % of the
  time on 31/10–2/11.
- **The split can only dry.** Cooling condenses water on the coil, so holding the
  wine cold and holding it humid pull against each other. Temperature wins
  (stability matters most for red wine); the setpoint is relaxed a little only
  while the air is too dry, and never above the season's ceiling. v0.91.0: above
  the ceiling the dry-air relax is dropped and the head is pushed `WARM_PUSH`
  below the setpoint (never under the unit's 18 °C floor) — its own probe reads
  ~2 °C under the mixed air (19 vs 21–22 °C), so the plain setpoint left it idle.

Season windows (`CELLAR_WINDOWS`): the wine wants 12–14 °C and 60–70 % RH; the
unit cannot go below 18 °C. Summer = hold the floor (18), tolerate up to 20.
Winter = the cellar cools by itself; the split only trims above 18 and nothing
heats (a heat call would fight the shared outdoor unit) — below the low edge we
only warn. The other way round, when someone heats the GARAGE (owner-manual),
the wine head switches OFF so the shared outdoor unit can heat (v0.88.0). Humidity is the same year-round: dry above 70 %, relax below 55 %.
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
REASON_WARM = "troppo_calda"           # v0.91.0: above t_high, push the head
REASON_NO_SAMPLE = "senza_misura"
REASON_YIELD_HEAT = "cede_al_garage"   # v0.88.0: a manual heat call owns the outdoor unit

HEAD_FLOOR = 18.0        # the Daikin head's lowest cooling setpoint
WARM_PUSH = 1.0          # °C under the setpoint while the cellar is above t_high
WARM_HYSTERESIS = 0.5    # stays "warm" until this far under t_high
SAMPLE_MEAN_MAX_N = 40   # ~20 min of 30 s ticks; beyond, a moving average


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
    run: datetime | None = None   # v0.91.0: fan run the mean belongs to
    n: int = 1                    # readings in that mean


@dataclass(frozen=True)
class CellarDecision:
    mode: str                # "cool" | "dry" | "off" (yielding to a heat call)
    setpoint: float | None   # None for dry
    drying: bool             # hysteresis state to carry
    relaxed: bool            # hysteresis state to carry
    reason: str
    warm: bool = False       # hysteresis state to carry (v0.91.0)


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
    """Take the reading only once the fan has mixed the air; else keep `prev`.

    Within one run (same `fan_since`) the readings are averaged — a running mean,
    a moving average past `SAMPLE_MEAN_MAX_N` (a drying run lasts hours)."""
    if (
        fan_on and fan_since is not None and now - fan_since >= mix_min
        and temp is not None and rh is not None
    ):
        if prev is None or prev.run != fan_since:
            return CellarSample(temp, rh, now, fan_since, 1)
        n = min(prev.n + 1, SAMPLE_MEAN_MAX_N)
        return CellarSample(
            prev.temp + (temp - prev.temp) / n, prev.rh + (rh - prev.rh) / n,
            now, fan_since, n,
        )
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
    warm: bool = False,
) -> CellarDecision:
    """Mode + setpoint for the wine head.

    - RH above the ceiling: dry. `dry` while the air is already at/below the
      setpoint (it barely cools), else `cool` (cools AND dries). Stays on until
      RH falls `hysteresis` below the ceiling.
    - RH below the floor: hold `cool`, setpoint raised by `relax` (capped at
      `t_high`) so the coil runs — and dries — less. Ends `hysteresis` above.
    - v0.91.0, above `t_high` (until `WARM_HYSTERESIS` under it): temperature
      outranks dry air — no relax, setpoint `WARM_PUSH` lower (>= HEAD_FLOOR).
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
    warm = sample.temp > window.t_high or (
        warm and sample.temp > window.t_high - WARM_HYSTERESIS
    )
    if drying:
        if sample.temp <= window.setpoint + 0.5:
            return CellarDecision("dry", None, True, False, REASON_DRYING, warm)
        return CellarDecision("cool", window.setpoint, True, False, REASON_DRYING, warm)
    if warm:
        sp = max(window.setpoint - WARM_PUSH, HEAD_FLOOR)
        return CellarDecision("cool", sp, False, False, REASON_WARM, True)
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
