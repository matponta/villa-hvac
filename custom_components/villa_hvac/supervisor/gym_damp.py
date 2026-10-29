"""Pure palestra + bagno palestra damp core (v0.90.0, STORY_GYM_DAMP.md).

The two rooms are one air volume (the door between them is habitually open),
with old masonry against the soil and a shower. Two tools dry them:

- the bathroom DEHUMIDIFIER on a Shelly plug (~250 W, continuous drain) — the
  12–16/10/2026 natural experiment proved it alone, door open, dries BOTH rooms
  (bagno 72 → 50 %, palestra 80 → 60 %);
- the palestra SPLIT in `dry` (cool-side, shares the trio's outdoor unit).

Rules (owner + physics):

- **Control on the KNX pair, never on the EP1.** The EP1 3febdc self-heats and
  reads 15–25 points low all year. `rh_ctrl = max(palestra, bagno)`; one KNX
  sensor stale → the other; both stale → the UP Sense on the stairs door;
  everything blind → hold the last state for `blind_hold`, then stop.
- **Winter: dehumidifier only.** `dry` cools the room and fights the radiant;
  the dehumidifier's ~250 W is a bonus.
- **Summer: split first.** It removes heat AND water; the dehumidifier dumps
  its heat into a room being cooled, so it joins only after `stage2_delay` of
  split-alone above the ceiling, or when the split can't help (yielding to a
  garage heat call, unavailable, or not ours to drive).
- Hysteresis: start at `rh_ctrl ≥ ceiling`, stop at `rh_ctrl ≤ release`.
- Home/away/Vacanza-agnostic: the walls don't care who is home.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

# Reasons (Italian — they surface verbatim as the sensor state).
REASON_DRY = "asciutto"
REASON_DEHUM = "deumidifica"
REASON_SPLIT_DRY = "split_dry"
REASON_SPLIT_AND_DEHUM = "split_dry+deumidificatore"
REASON_SPLIT_BUSY = "split_occupato"        # the split yields to a garage heat call
REASON_FALLBACK = "sensore_ripiego"         # deciding on the UP Sense
REASON_BLIND = "sensori_ko"
REASON_MANUAL = "manuale"                   # set by the HA side (arbiter backoff)
REASON_DISABLED = "disattivo"               # set by the HA side (switch off)

SOURCE_KNX = "knx"
SOURCE_KNX_PALESTRA = "knx_palestra"
SOURCE_KNX_BAGNO = "knx_bagno"
SOURCE_FALLBACK = "up_sense"
SOURCE_NONE = "nessuno"


def dew_point(temp_c: float | None, rh: float | None) -> float | None:
    """Dew point in °C (Magnus, same constants as `absolute_humidity`)."""
    if temp_c is None or rh is None:
        return None
    if not (math.isfinite(temp_c) and math.isfinite(rh)) or not 0.0 < rh <= 100.0:
        return None
    g = math.log(rh / 100.0) + 17.67 * temp_c / (temp_c + 243.5)
    return 243.5 * g / (17.67 - g)


def gym_rh_source(
    rh_palestra: float | None, rh_bagno: float | None, rh_fallback: float | None,
) -> tuple[float | None, str]:
    """The controlling RH and where it came from. Inputs are already
    freshness-filtered (None = stale/unavailable). The EP1 is never an input."""
    if rh_palestra is not None and rh_bagno is not None:
        return max(rh_palestra, rh_bagno), SOURCE_KNX
    if rh_palestra is not None:
        return rh_palestra, SOURCE_KNX_PALESTRA
    if rh_bagno is not None:
        return rh_bagno, SOURCE_KNX_BAGNO
    if rh_fallback is not None:
        return rh_fallback, SOURCE_FALLBACK
    return None, SOURCE_NONE


@dataclass(frozen=True)
class GymDampDecision:
    active: bool                 # hysteresis state to carry (the zone wants drying)
    dehum: bool                  # dehumidifier plug wanted ON
    split_humidity: str | None   # "dry" -> the palestra split dries; None = not asked
    reason: str
    rh_ctrl: float | None
    source: str


def gym_damp_decision(
    rh_palestra: float | None,
    rh_bagno: float | None,
    rh_fallback: float | None,
    *,
    summer: bool,
    active: bool,
    split_available: bool,
    split_yielding: bool,
    stage2_elapsed: timedelta | None,
    stage2_delay: timedelta,
    blind_for: timedelta | None,
    blind_hold: timedelta,
    ceiling: float,
    release: float,
) -> GymDampDecision:
    """One cycle of the damp law. `active` is the carried hysteresis state;
    `stage2_elapsed` is how long the split has been drying alone above the
    ceiling (None = not running); `blind_for` how long every sensor has been
    out (None = not blind)."""
    rh, source = gym_rh_source(rh_palestra, rh_bagno, rh_fallback)
    if rh is None:
        if blind_for is not None and blind_for >= blind_hold:
            return GymDampDecision(False, False, None, REASON_BLIND, None, source)
        # hold: keep the last state, decided as if the reading hadn't moved
        decision = _act(
            active, summer=summer, split_available=split_available,
            split_yielding=split_yielding, stage2_elapsed=stage2_elapsed,
            stage2_delay=stage2_delay, rh=None, source=source,
        )
        return GymDampDecision(
            decision.active, decision.dehum, decision.split_humidity,
            REASON_BLIND, None, source,
        )
    want = rh >= ceiling or (active and rh > release)
    decision = _act(
        want, summer=summer, split_available=split_available,
        split_yielding=split_yielding, stage2_elapsed=stage2_elapsed,
        stage2_delay=stage2_delay, rh=rh, source=source,
    )
    if source == SOURCE_FALLBACK:
        return GymDampDecision(
            decision.active, decision.dehum, decision.split_humidity,
            REASON_FALLBACK, rh, source,
        )
    return decision


def _act(
    want: bool, *, summer: bool, split_available: bool, split_yielding: bool,
    stage2_elapsed: timedelta | None, stage2_delay: timedelta,
    rh: float | None, source: str,
) -> GymDampDecision:
    if not want:
        return GymDampDecision(False, False, None, REASON_DRY, rh, source)
    if not summer:
        return GymDampDecision(True, True, None, REASON_DEHUM, rh, source)
    if split_yielding:
        return GymDampDecision(True, True, None, REASON_SPLIT_BUSY, rh, source)
    if not split_available:
        return GymDampDecision(True, True, None, REASON_DEHUM, rh, source)
    if stage2_elapsed is not None and stage2_elapsed >= stage2_delay:
        return GymDampDecision(True, True, "dry", REASON_SPLIT_AND_DEHUM, rh, source)
    return GymDampDecision(True, False, "dry", REASON_SPLIT_DRY, rh, source)
