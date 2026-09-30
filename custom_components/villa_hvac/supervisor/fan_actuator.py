"""Pure fancoil-unit actuator (v0.72.0, system review 2026-09-30 §2).

A fancoil unit is THREE coupled bus objects — the `manuale` switch, the fan's
ON/OFF object and its speed % — and up to eight writers (KNX AUTO, the living
governor, #2b night silence, the rack/P1 guards, the stranded-fan watchdog, the
fail-safe, the legacy buonanotte automation, the wall panel). Every live fan
incident of the summer (dead fan at wake v0.56, orphaned manuale v0.68,
throttled guard v0.67) was an INTERACTION between those writers, patched per
controller. This module states the unit invariants ONCE, between the priority
merge and the per-lever reconcile, so a future controller is safe by
construction instead of by remembering the rules:

I1 re-arm   — a fan the SUPERVISOR switched OFF must be switched back ON as soon
              as nobody holds an opinion on it and the unit is (going) back in
              AUTO: KNX AUTO never restarts a fan whose ON/OFF object was written
              OFF, and the interlock then keeps the valve shut (the invisible
              dead zone). Deferred while the zone is window-paused / free-cooling
              (no air into an open window); it fires when the pause ends. The
              fan leaves the "turned off" set only when a live read CONFIRMS it
              ON (not on write — a dropped KNX telegram must not lose it).
I2 ordering — within a unit, `manuale → on` is written before the %, and
              `manuale → off` after it, so a % is never written into AUTO (where
              KNX would immediately re-drive it) on the entering edge, and a
              hand-back leaves a LIVE fan behind it.
I3 provenance — every unit lever carries the name of the controller/policy that
              won it, so "who is driving this fan?" is answerable (explain
              sensors, v0.75.0).

Import-pure: no Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .arbiter import fan_lever, switch_lever

REARM_OWNER = "fan_actuator:rearm"


@dataclass(frozen=True)
class FanUnit:
    """One fancoil unit as the actuator sees it this cycle."""

    zone_id: str
    fan: str
    manuale: str
    defer_rearm: bool = False     # zone paused (#4 / free-air) or free-cooling


@dataclass(frozen=True)
class FanLive:
    """Live bus reads for one unit (None = transient/unknown)."""

    fan_on: bool | None
    manuale_on: bool | None


@dataclass(frozen=True)
class FanResolution:
    desired: dict
    owners: dict
    confirmed_alive: frozenset[str] = frozenset()
    rearmed: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    notes: dict = field(default_factory=dict)     # fan -> diagnostic label
    unit_owner: dict = field(default_factory=dict)  # fan -> who drives it


def _is_on(value) -> bool:
    return str(value) == "on"


def _pct(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_fan_units(
    desired: dict,
    owners: dict,
    units: list[FanUnit],
    live: dict[str, FanLive],
    turned_off: set[str] | frozenset[str],
    *,
    rearm_pct: int,
) -> FanResolution:
    """Apply I1–I3 to the merged desired map. Pure; never drops an opinion."""
    out = dict(desired)
    own = dict(owners)
    alive: set[str] = set()
    rearmed: list[str] = []
    deferred: list[str] = []
    notes: dict[str, str] = {}
    unit_owner: dict[str, str | None] = {}
    by_lever: dict[str, FanUnit] = {}

    for u in units:
        fk, sk = fan_lever(u.fan), switch_lever(u.manuale)
        by_lever[fk] = u
        by_lever[sk] = u
        lv = live.get(u.fan, FanLive(None, None))
        sw_want = out.get(sk)
        fan_want = out.get(fk)
        # Will the unit be in AUTO after this cycle's writes?
        will_be_auto = (
            (sk in out and sw_want is not None and not _is_on(sw_want))
            or (sw_want is None and lv.manuale_on is False)
        )
        if u.fan in turned_off and lv.fan_on is True:
            alive.add(u.fan)                       # I1: confirmed alive
        elif (
            u.fan in turned_off and lv.fan_on is False
            and fan_want is None and will_be_auto
        ):
            if u.defer_rearm:
                deferred.append(u.fan)
            else:
                out[fk] = rearm_pct                 # I1: re-arm
                own[fk] = REARM_OWNER
                rearmed.append(u.fan)
        pct = _pct(out.get(fk))
        if pct is not None and pct > 0 and will_be_auto:
            notes[u.fan] = (
                "handback" if sk in out and sw_want is not None else "pct_in_auto"
            )
        if fk in out and out[fk] is not None:
            unit_owner[u.fan] = own.get(fk)
        elif sk in out and _is_on(out.get(sk)):
            unit_owner[u.fan] = own.get(sk)
        elif lv.manuale_on is False:
            unit_owner[u.fan] = "knx_auto"
        elif lv.manuale_on is True:
            unit_owner[u.fan] = "manual"     # held by someone outside the arbiter
        else:
            unit_owner[u.fan] = None

    # I2: rebuild the map with each unit's levers in a safe write order.
    ordered: dict = {}
    emitted: set[str] = set()
    for lever in out:
        u = by_lever.get(lever)
        if u is None:
            ordered[lever] = out[lever]
            continue
        if u.fan in emitted:
            continue
        emitted.add(u.fan)
        fk, sk = fan_lever(u.fan), switch_lever(u.manuale)
        if sk in out and _is_on(out[sk]):
            ordered[sk] = out[sk]
        if fk in out:
            ordered[fk] = out[fk]
        if sk in out and not _is_on(out[sk]):
            ordered[sk] = out[sk]
    # Levers injected for units not present in the original key order.
    for lever, value in out.items():
        if lever not in ordered:
            ordered[lever] = value

    return FanResolution(
        desired=ordered,
        owners=own,
        confirmed_alive=frozenset(alive),
        rearmed=tuple(rearmed),
        deferred=tuple(deferred),
        notes=notes,
        unit_owner=unit_owner,
    )
