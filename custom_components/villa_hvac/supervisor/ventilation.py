"""Pure VMC year-round core (v0.86.0, STORY_VMC.md).

The two Salda Smarty units now expose speed (33/66/100 %), the unit's own
outdoor/supply/extract air temperatures, filter timer and alarm/warning. This
module holds the decisions that use them, HA-free:

- `absolute_humidity`: g/m³ from temperature + RH (Magnus). Ventilation moves
  WATER, not RH: outside air at 90 % RH and 8 °C is DRIER than a 50 % RH room at
  20 °C. Every humidity decision compares absolute humidity.
- `vmc_dehumidify`: the ground-floor unit serves earth-sheltered rooms (1-2
  walls against soil). Raise its speed when a room is above its RH ceiling AND
  the outside air is drier in absolute terms; report "damp outside" when the
  outside air would ADD water, which also vetoes the summer night flush there
  (warm humid air onto cold earth walls = condensation).
- `vmc_speed_target`: the speed this controller wants to own (None = hand back).
- `recovery_efficiency` / `bypass_state`: heat-recovery diagnostics.
- `Held` + `hold_value`: the Smarty integration drops to `unavailable` for ~2 s
  every few minutes — a reading survives a blip for `max_age`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

VMC_PCT_MIN = 33
VMC_PCT_MID = 66
VMC_PCT_MAX = 100

# Reasons (Italian — they surface verbatim as the unit status sensor state).
REASON_BASE = "base"
REASON_DEHUMIDIFY = "deumidifica"
REASON_DEHUMIDIFY_HIGH = "deumidifica_forte"
REASON_QUIET_FLUSH = "lavaggio_silenzioso"
REASON_DAMP_OUTSIDE = "umido_fuori"
REASON_CELLAR_DRY = "cantina_secca"
REASON_NO_DATA = "dati_mancanti"


def absolute_humidity(temp_c: float | None, rh: float | None) -> float | None:
    """Water vapour density in g/m³ (Magnus formula, ±0.1 g/m³ for 0-40 °C)."""
    if temp_c is None or rh is None:
        return None
    if not (math.isfinite(temp_c) and math.isfinite(rh)) or not 0.0 <= rh <= 100.0:
        return None
    sat_hpa = 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))
    return sat_hpa * rh * 2.1674 / (273.15 + temp_c)


@dataclass(frozen=True)
class RoomAir:
    """One served room's humidity picture this cycle."""

    zone: str
    temp: float | None
    rh: float | None
    rh_ceiling: float           # at/above: the room wants drying
    rh_floor: float | None = None  # below: the room must not be dried further

    @property
    def ah(self) -> float | None:
        return absolute_humidity(self.temp, self.rh)


@dataclass(frozen=True)
class DehumidifyDecision:
    pct: int | None          # None = no drying wanted
    reason: str
    zone: str | None         # the room driving the decision
    damp_outside: bool       # outside air would ADD water to that room
    outdoor_ah: float | None
    room_ah: float | None


def vmc_dehumidify(
    rooms: list[RoomAir],
    *,
    outdoor_temp: float | None,
    outdoor_rh: float | None,
    active: bool,
    margin: float,
    hysteresis: float,
    release_delta: float,
    rh_very_high: float,
) -> DehumidifyDecision:
    """Should a damp-room unit run faster to dry its rooms?

    The driving room is the one furthest above (or closest to) its ceiling.
    Start: RH >= ceiling AND outdoor AH <= room AH - `margin`. While `active`
    both relax (RH >= ceiling - `release_delta`, AH gap >= margin - hysteresis)
    so the speed doesn't flap. RH >= `rh_very_high` -> full speed, unless a
    room below its `rh_floor` (the wine cellar) shares the unit: then it stays
    at mid speed. Mould beats dry corks, so the floor caps, it never vetoes.
    `damp_outside` is reported independently of the RH trigger: it is the
    night-flush veto for earth-sheltered rooms.
    """
    known = [r for r in rooms if r.ah is not None]
    oah = absolute_humidity(outdoor_temp, outdoor_rh)
    if not known:
        return DehumidifyDecision(None, REASON_NO_DATA, None, False, oah, None)
    worst = max(known, key=lambda r: r.rh - r.rh_ceiling)
    wah = worst.ah
    damp = oah is not None and oah >= wah
    trigger = worst.rh_ceiling - (release_delta if active else 0.0)
    if worst.rh < trigger:
        return DehumidifyDecision(None, REASON_BASE, worst.zone, damp, oah, wah)
    if oah is None:
        return DehumidifyDecision(None, REASON_NO_DATA, worst.zone, False, None, wah)
    gap = margin - (hysteresis if active else 0.0)
    if oah > wah - gap:
        return DehumidifyDecision(None, REASON_DAMP_OUTSIDE, worst.zone, damp, oah, wah)
    if worst.rh < rh_very_high:
        return DehumidifyDecision(
            VMC_PCT_MID, REASON_DEHUMIDIFY, worst.zone, damp, oah, wah,
        )
    if any(r.rh_floor is not None and r.rh < r.rh_floor for r in known):
        # a room that must not get drier shares the unit: dry, but not flat out
        return DehumidifyDecision(
            VMC_PCT_MID, REASON_CELLAR_DRY, worst.zone, damp, oah, wah,
        )
    return DehumidifyDecision(
        VMC_PCT_MAX, REASON_DEHUMIDIFY_HIGH, worst.zone, damp, oah, wah,
    )


def vmc_speed_target(
    *,
    dehumidify_pct: int | None,
    flush_wanted: bool,
    quiet: bool,
    quiet_pct: int,
) -> tuple[int | None, str]:
    """The speed this controller wants to OWN, or None to hand the unit back.

    - drying (any season) asks for its own speed;
    - a summer night flush the quiet veto blocks (bedroom unit, occupied Notte)
      runs at `quiet_pct` instead of not at all;
    - the higher of the two wins. Nothing else ever lowers the owner's speed:
      the baseline is whatever the owner/the unit's own program set.
    """
    candidates: list[tuple[int, str]] = []
    if dehumidify_pct is not None:
        candidates.append((
            dehumidify_pct,
            REASON_DEHUMIDIFY_HIGH if dehumidify_pct >= VMC_PCT_MAX
            else REASON_DEHUMIDIFY,
        ))
    if flush_wanted and quiet and quiet_pct > VMC_PCT_MIN:
        candidates.append((quiet_pct, REASON_QUIET_FLUSH))
    if not candidates:
        return None, REASON_BASE
    return max(candidates, key=lambda c: c[0])


def recovery_efficiency(
    outdoor: float | None, supply: float | None, extract: float | None,
    *, min_spread: float,
) -> float | None:
    """Supply-side temperature efficiency (supply-out)/(extract-out).

    None when the indoor/outdoor spread is too small to mean anything (the
    sensors are 0.1 °C, a 2 °C spread would be ±10 %).
    """
    if outdoor is None or supply is None or extract is None:
        return None
    spread = extract - outdoor
    if abs(spread) < min_spread:
        return None
    return (supply - outdoor) / spread


def bypass_state(efficiency: float | None) -> str | None:
    """Infer the summer bypass from the efficiency (the integration exposes no
    bypass entity): supply ≈ outdoor -> open, a working exchanger -> closed."""
    if efficiency is None:
        return None
    if efficiency < 0.3:
        return "aperto"
    if efficiency >= 0.6:
        return "chiuso"
    return None


@dataclass(frozen=True)
class Held:
    """A reading kept across short `unavailable` blips."""

    value: float
    at: datetime


def hold_value(
    prev: Held | None, raw: float | None, now: datetime, *, max_age: timedelta,
) -> Held | None:
    """Fresh reading -> take it; missing -> keep the last one up to `max_age`."""
    if raw is not None:
        return Held(raw, now)
    if prev is not None and now - prev.at <= max_age:
        return prev
    return None


def pct_matches(live: float | None, target: int, tolerance: float = 5.0) -> bool:
    """Smarty reports 33/66/100 (or 67): compare with a tolerance."""
    return live is not None and abs(live - target) <= tolerance
