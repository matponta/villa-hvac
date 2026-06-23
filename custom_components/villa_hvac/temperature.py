"""Temperature fusion logic for #1 (pure, unit-testable).

Kept free of Home Assistant imports so the priority/staleness rules can be
tested in isolation. The coordinator builds `TempSource`s from live state and
calls `fuse_temperature`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TempSource:
    """A candidate temperature reading for one zone.

    `value` is the temperature in °C (or None if missing/unparseable).
    `age_s` is seconds since the source last updated (or None if unknown).
    """

    label: str
    value: float | None
    age_s: float | None


def fuse_temperature(
    sources: list[TempSource], max_age_s: float
) -> tuple[float | None, str | None]:
    """Return (value, label) of the first usable source, else (None, None).

    Sources are tried in priority order. A source is usable only when it has a
    numeric value AND a known age within `max_age_s` (older readings are stale
    and skipped, so a fresh fallback wins over a stale primary).
    """
    for src in sources:
        if src.value is None or src.age_s is None:
            continue
        if src.age_s > max_age_s:
            continue
        return src.value, src.label
    return None, None
