"""Pure per-facade effective solar (S_eff — STORY_SEFF).

The ONE place the S_eff law exists: per-zone effective irradiance COMPUTED
(never learned) from sun geometry, verified facade normals (cover orientation
labels), and live cover position. Everything downstream — estimator ingest,
live fan sizing, planner sims, PV ranking, diagnostics — consumes this output
and never re-derives it. Import-pure (math + dataclasses only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import CoverInfo

# Facade normals, compass deg (0 = N). VERIFIED ONLY — the villa's rotation is
# non-uniform (225 vs 292 differ 67°), so east/north are NOT derivable as
# complements. A cover whose orientation label is absent here is EXCLUDED from
# the facade set (its zone may then fall back to GHI identity).
SEFF_FACADE_NORMALS: dict[str, float] = {
    "south": 225.0,   # verified live: "south" label = real SW facade
    "west": 292.0,    # verified live: "west" label = real WNW facade
}

SEFF_DIFFUSE_FRACTION = 0.25    # fixed GHI beam/diffuse split (clear-summer)
SEFF_DIFFUSE_VERTICAL = 0.22    # diffuse on vertical: sky view + ground albedo
SEFF_BEAM_MIN_ELEVATION = 3.0   # deg: below this the beam term is 0 (1/sin ill-conditioned)
SEFF_RB_MAX = 3.0               # clamp on the tilt factor (per facade AND zone beam sum)
SEFF_COVER_FLOOR = 0.2          # owner-fixed: a fully-closed cover still transmits ~20%

SEFF_UNITS_GHI = "ghi"          # units-tag for GHI-identity zones (see units_tag)
SEFF_UNITS_PREFIX = "seff1:"

# Per-cycle source quality (ZoneSnapshot.s_eff_source). The estimator learns
# ONLY from "facade" and "ghi" samples: "fallback" (sun/GHI missing → control
# degrades to GHI, over-stating gain = comfort-safe) and "facade_degraded"
# (a cover position unknown → g=1 assumed; learning it would fit b
# systematically LOW because position dropouts correlate with shading hours)
# both keep b's units pure by being skipped.
SEFF_SOURCE_FACADE = "facade"
SEFF_SOURCE_DEGRADED = "facade_degraded"
SEFF_SOURCE_GHI = "ghi"
SEFF_SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class Aperture:
    """One glazed facade of a zone: outward normal + live cover transmission.

    `transmission` is the MEAN cover_transmission() over the facade's covers
    (two covers on one facade shade one aperture set — summing would
    double-count the facade). `cover_count` feeds the units tag: adding a
    cover to a fitted facade changes the mean-g scale, which must re-wipe b.
    `degraded` marks that a position was unknown this cycle (g=1 assumed).
    """

    normal_deg: float
    transmission: float
    cover_count: int = 1
    degraded: bool = False


def facade_beam_factor(
    sun_el_deg: float, sun_az_deg: float, normal_deg: float
) -> float:
    """Direct-beam tilt factor for VERTICAL glazing (R_b at tilt 90°):
    cos(incidence)/sin(elevation), 0 behind the facade, 0 below 3° elevation
    (the 1/sin is ill-conditioned there), clamped at SEFF_RB_MAX (air mass,
    frame shading and glass reflectance make larger values unphysical). The
    1/sin(el) amplification IS the late-afternoon phenomenon this law exists
    to model — a low sun square-on a WNW pane delivers more per m² than the
    horizontal pyranometer reads."""
    if sun_el_deg <= SEFF_BEAM_MIN_ELEVATION:
        return 0.0
    el = math.radians(sun_el_deg)
    cos_i = math.cos(el) * math.cos(math.radians(sun_az_deg - normal_deg))
    if cos_i <= 0:
        return 0.0
    return min(cos_i / math.sin(el), SEFF_RB_MAX)


def cover_transmission(position: float | None) -> float:
    """Solar transmission of a cover at an HA position (0 = fully closed/down,
    100 = open; verified). None (unknown this cycle) → 1.0: assume open
    over-states gain → fan sized up → the comfort-safe direction (never treat
    None as closed — that would starve the fan exactly while shading is
    active). Linear with the owner-fixed 0.2 closed floor; the learned
    per-room b absorbs the scale, so only monotonicity and the floor are
    load-bearing."""
    if position is None:
        return 1.0
    p = max(0.0, min(100.0, float(position)))
    return SEFF_COVER_FLOOR + (1.0 - SEFF_COVER_FLOOR) * p / 100.0


def zone_apertures(
    covers: tuple[CoverInfo, ...] | list[CoverInfo],
) -> dict[str, tuple[Aperture, ...]]:
    """Group shadeable covers into per-zone apertures: one Aperture per
    (zone, verified facade normal), transmission = mean over that facade's
    covers, degraded when any position is unknown. Covers with an unmapped
    orientation label or no zone are excluded (an emptied set → GHI identity).
    `blocked` covers still contribute with their real position — blocked means
    "don't actuate", not "no glass"."""
    grouped: dict[str, dict[float, list[CoverInfo]]] = {}
    for c in covers:
        normal = SEFF_FACADE_NORMALS.get(c.orientation)
        if normal is None or not c.zone:
            continue
        grouped.setdefault(c.zone, {}).setdefault(normal, []).append(c)
    return {
        zone: tuple(
            Aperture(
                normal_deg=normal,
                transmission=(
                    sum(cover_transmission(c.current_position) for c in cs) / len(cs)
                ),
                cover_count=len(cs),
                degraded=any(c.current_position is None for c in cs),
            )
            for normal, cs in sorted(facades.items())
        )
        for zone, facades in grouped.items()
    }


def zone_effective_solar(
    ghi: float | None,
    sun_el: float | None,
    sun_az: float | None,
    apertures: tuple[Aperture, ...],
) -> tuple[float | None, str]:
    """(S_eff in W/m²-equivalent GHI scale, source). The zone BEAM SUM is
    clamped at SEFF_RB_MAX: with two facades 67° apart a low sun between the
    normals can drive both per-facade factors to the clamp (~4.9×GHI summed) —
    the zone bound is 0.75·3.0 + n_facades·0.22·g (≤ 2.69×GHI for 2 facades).
    The diffuse term stays in S_eff (not folded into c): it is GHI-correlated,
    not constant — folding it into c would alias cloudy-vs-clear days into the
    intercept."""
    if ghi is None or not math.isfinite(ghi):
        return None, SEFF_SOURCE_FALLBACK
    if not apertures:
        return ghi, SEFF_SOURCE_GHI
    source = (
        SEFF_SOURCE_DEGRADED
        if any(ap.degraded for ap in apertures)
        else SEFF_SOURCE_FACADE
    )
    if (
        sun_el is None or sun_az is None
        or not math.isfinite(sun_el) or not math.isfinite(sun_az)
    ):
        return ghi, SEFF_SOURCE_FALLBACK
    if sun_el <= 0:
        return 0.0, source
    beam_sum = sum(
        ap.transmission * facade_beam_factor(sun_el, sun_az, ap.normal_deg)
        for ap in apertures
    )
    diffuse_sum = sum(ap.transmission * SEFF_DIFFUSE_VERTICAL for ap in apertures)
    s_eff = ghi * (
        (1.0 - SEFF_DIFFUSE_FRACTION) * min(beam_sum, SEFF_RB_MAX) + diffuse_sum
    )
    return s_eff, source


def units_tag(apertures: tuple[Aperture, ...]) -> str:
    """Stable per-zone semantics stamp the migration compares (STORY_SEFF §4):
    "ghi" for an empty set, else "seff1:<normal>x<count>,…" sorted by normal —
    e.g. "seff1:225x1,292x1" (main_bedroom). The cover MULTISET is
    load-bearing: a second cover on an already-fitted facade changes the
    per-facade mean-g scale, so it must flip the tag and re-wipe b."""
    if not apertures:
        return SEFF_UNITS_GHI
    return SEFF_UNITS_PREFIX + ",".join(
        f"{int(ap.normal_deg)}x{ap.cover_count}"
        for ap in sorted(apertures, key=lambda ap: ap.normal_deg)
    )


def zone_solar_curves(
    ghi_curve: list[float],
    elevations: list[float],
    azimuths: list[float],
    apertures_by_zone: dict[str, tuple[Aperture, ...]],
    live_ratio: dict[str, float] | None = None,
) -> dict[str, list[float]]:
    """Per-step future S_eff per zone for the planner horizon, cover positions
    frozen at their current values (v1: under the never-raise shading
    invariant positions ratchet DOWN during the day, so hold-current
    under-states future shading → over-states gain → comfort-safe).

    Flat mode (empty elevations/azimuths — solar forecast off, astral missing,
    or the runtime fallback): the planner must NOT silently sim on the house
    curve while actuation runs on S_eff, so each zone's flat curve is
    ghi_curve × live_ratio[zone] (current live geometry propagated). Zones
    without a ratio are OMITTED from the result — callers fall back via
    .get(zone, house_curve) and must mark that zone's plan solar_domain="ghi"
    so the domain divergence is visible, never silent (STORY_SEFF §1.4).
    Fallback-source steps use the GHI step value."""
    if not ghi_curve:
        return {}
    if not elevations or not azimuths:
        if not live_ratio:
            return {}
        return {
            zone: [g * ratio for g in ghi_curve]
            for zone, ratio in live_ratio.items()
            if zone in apertures_by_zone
        }
    curves: dict[str, list[float]] = {}
    for zone, apertures in apertures_by_zone.items():
        steps: list[float] = []
        for i, ghi in enumerate(ghi_curve):
            el = elevations[min(i, len(elevations) - 1)]
            az = azimuths[min(i, len(azimuths) - 1)]
            value, source = zone_effective_solar(ghi, el, az, apertures)
            steps.append(ghi if source == SEFF_SOURCE_FALLBACK or value is None else value)
        curves[zone] = steps
    return curves
