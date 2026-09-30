"""Pure per-room actuation telemetry (v0.71.0, system review 2026-09-30 §1).

Why this exists: the recorder keeps raw history ~7 days, and fan %, valve
states, presets and modes carry no state_class — so they never reach HA's
long-term statistics. After a week nobody (owner or Claude) can reason about
how the fans and valves actually behaved. These rolling aggregates are exposed
as MEASUREMENT sensors so HA's hourly statistics keep them forever:

- valve duty   : time-weighted fraction the EV FAN valve was OPEN over the window
- valve strokes: CLOSED→OPEN edges in the window (valve chatter / bang-bang)
- fan delivered: the airflow actually delivered (0 when the fan object is OFF)

Import-pure (no HA): the engine feeds one sample per zone per cycle.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

TELEMETRY_WINDOW = timedelta(minutes=60)
# A gap longer than this between two samples is NOT credited to either state
# (restart / outage we did not observe) — mirrors the runtime KPI's 3x-poll rule.
TELEMETRY_MAX_GAP = timedelta(seconds=90)


@dataclass(frozen=True)
class ValveStats:
    """Rolling valve aggregates over the telemetry window."""

    duty: float | None      # 0..1, None = no observed time in the window
    strokes: int            # CLOSED→OPEN edges inside the window
    observed_s: float       # seconds of credited observation in the window


class ValveWindow:
    """Rolling window of (timestamp, open) valve samples."""

    def __init__(
        self, window: timedelta = TELEMETRY_WINDOW,
        max_gap: timedelta = TELEMETRY_MAX_GAP,
    ) -> None:
        self._window = window
        self._max_gap = max_gap
        self._samples: deque[tuple[datetime, bool | None]] = deque()

    def add(self, now: datetime, is_open: bool | None) -> None:
        if self._samples and now <= self._samples[-1][0]:
            return  # out-of-order / duplicate tick: ignore
        self._samples.append((now, is_open))
        # Keep one sample older than the window start so the interval that
        # straddles the boundary can still be credited (clipped) to its state.
        start = now - self._window
        while len(self._samples) >= 2 and self._samples[1][0] <= start:
            self._samples.popleft()

    def stats(self, now: datetime) -> ValveStats:
        start = now - self._window
        open_s = 0.0
        seen_s = 0.0
        strokes = 0
        samples = list(self._samples)
        prev_state: bool | None = None
        for i, (ts, state) in enumerate(samples):
            end = samples[i + 1][0] if i + 1 < len(samples) else now
            # An edge counts when it happens inside the window, between two
            # KNOWN readings (unknown → open is not evidence of a stroke).
            if ts >= start and state is True and prev_state is False:
                strokes += 1
            if state is not None:
                prev_state = state
            if state is None or end - ts > self._max_gap:
                continue
            lo = max(ts, start)
            if end <= lo:
                continue
            span = (end - lo).total_seconds()
            seen_s += span
            if state:
                open_s += span
        duty = open_s / seen_s if seen_s > 0 else None
        return ValveStats(duty=duty, strokes=strokes, observed_s=seen_s)


@dataclass(frozen=True)
class UnitTelemetry:
    """What the dashboard / explain surface reads for one fancoil unit."""

    zone_id: str
    valve_duty: float | None     # 0..1 over the window
    valve_strokes: int
    valve_open: bool | None      # live
    fan_delivered: int | None    # live delivered % (0 = OFF object)
    manuale_on: bool | None      # live
    observed_s: float


class HouseTelemetry:
    """All per-unit windows. `observe` is called once per engine cycle (even
    deploy-dark — it is read-only), `view` builds the per-zone snapshot."""

    def __init__(self, window: timedelta = TELEMETRY_WINDOW) -> None:
        self._window = window
        self._valves: dict[str, ValveWindow] = {}
        self._live: dict[str, tuple[bool | None, int | None, bool | None]] = {}
        self._now: datetime | None = None

    def observe(
        self, now: datetime,
        units: dict[str, tuple[bool | None, int | None, bool | None]],
    ) -> None:
        """`units`: zone_id -> (valve_open, fan_delivered_pct, manuale_on)."""
        self._now = now
        for zid, (valve, fan, manuale) in units.items():
            self._valves.setdefault(zid, ValveWindow(self._window)).add(now, valve)
            self._live[zid] = (valve, fan, manuale)

    def get(self, zone_id: str) -> UnitTelemetry | None:
        if self._now is None or zone_id not in self._live:
            return None
        stats = self._valves[zone_id].stats(self._now)
        valve, fan, manuale = self._live[zone_id]
        return UnitTelemetry(
            zone_id=zone_id,
            valve_duty=stats.duty,
            valve_strokes=stats.strokes,
            valve_open=valve,
            fan_delivered=fan,
            manuale_on=manuale,
            observed_s=stats.observed_s,
        )
