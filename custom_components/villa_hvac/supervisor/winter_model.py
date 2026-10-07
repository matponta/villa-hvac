"""Winter radiant model (v0.80.0, STORY_WINTER_BRAIN W1) — pure, no HA imports.

Per room, driven by the radiant VALVE state (read-only signal):

    valve CLOSED (after the slab residual):  dT/dt = −a·(T − T_out)
    valve OPEN   (after the dead time):      dT/dt =  k_h − a·(T − T_out)

Learned from windows the house runs anyway (an observer: it never actuates):
  * `a`   on long valve-closed, low-sun windows, after RESIDUAL (the slab keeps
          emitting for a while after the valve closes);
  * `k_h` on valve-open, low-sun windows, after the dead time;
  * `lag` per valve-open episode: minutes until the room rose RISE_DETECT.
Each estimate is a running mean with a count; confidence = n/(n+N0); until a
parameter has samples the prior stands.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math

PRIOR_A = 0.03          # 1/h  — free cooling 0.3 °C/h at ΔT 10 °C
PRIOR_K = 0.6           # °C/h — radiant heating rate, valve held open
PRIOR_LAG_MIN = 45.0    # min  — valve open → room starts rising
BOUNDS_A = (0.005, 0.2)
BOUNDS_K = (0.05, 3.0)
BOUNDS_LAG = (5.0, 240.0)
N0 = 5                  # samples for 50 % confidence

SAMPLE_EVERY = timedelta(minutes=5)
RESIDUAL = timedelta(minutes=90)      # slab still emitting after the valve closed
MIN_WINDOW = timedelta(minutes=60)    # regression window length
MAX_SEGMENT = timedelta(hours=12)
MIN_DELTA_T = 3.0                     # °C indoor − outdoor for an `a` sample
LOW_SUN = 50.0                        # W/m² — sun would confound the fit
RISE_DETECT = 0.15                    # °C rise that ends the dead time
MAX_LAG_WAIT = timedelta(hours=4)


@dataclass(frozen=True)
class WinterParams:
    a: float = PRIOR_A
    k_h: float = PRIOR_K
    lag_min: float = PRIOR_LAG_MIN
    n_a: int = 0
    n_k: int = 0
    n_lag: int = 0

    def confidence(self) -> float:
        n = min(self.n_a, self.n_k)
        return n / (n + N0)


@dataclass
class _Segment:
    valve: bool
    start: datetime
    samples: list[tuple[datetime, float, float, float]] = field(default_factory=list)
    used_until: datetime | None = None    # last window end already learned
    min_temp: float | None = None         # for the lag (valve-open only)
    lag_done: bool = False
    rise1_at: datetime | None = None      # first +RISE_DETECT above the minimum


def _clamp(v: float, lo_hi: tuple[float, float]) -> float:
    return max(lo_hi[0], min(lo_hi[1], v))


def _running(mean: float, n: int, x: float) -> tuple[float, int]:
    return (mean * n + x) / (n + 1), n + 1


def _slope_per_h(samples) -> float | None:
    """Least-squares dT/dt (°C/h) over (t, T, ...) samples."""
    if len(samples) < 4:
        return None
    t0 = samples[0][0]
    xs = [(s[0] - t0).total_seconds() / 3600.0 for s in samples]
    ys = [s[1] for s in samples]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def recovery_minutes(
    temp: float | None, target: float | None, outdoor: float | None,
    p: WinterParams,
) -> float | None:
    """Minutes until `temp` reaches `target` with the valve held open (lag
    included). 0 when already there; None when unknown or unreachable."""
    if temp is None or target is None or outdoor is None:
        return None
    if temp >= target:
        return 0.0
    t_inf = outdoor + p.k_h / p.a
    if t_inf <= target:
        return None
    hours = math.log((t_inf - temp) / (t_inf - target)) / p.a
    return p.lag_min + hours * 60.0


def coast_temp(t0: float, outdoor: float, hours: float, p: WinterParams,
               floor: float | None = None) -> float:
    """Room temperature after `hours` with the valve closed (no sun), floored
    at the setback the thermostat would hold."""
    t = outdoor + (t0 - outdoor) * math.exp(-p.a * max(0.0, hours))
    return max(t, floor) if floor is not None else t


class WinterModel:
    """Per-room observer. `observe` once per cycle; never actuates."""

    def __init__(self) -> None:
        self.params: dict[str, WinterParams] = {}
        self._seg: dict[str, _Segment] = {}
        self._last_sample: dict[str, datetime] = {}

    def get(self, zone: str) -> WinterParams:
        return self.params.get(zone, WinterParams())

    # -- persistence -----------------------------------------------------------
    def dump(self) -> dict:
        return {
            z: {"a": p.a, "k_h": p.k_h, "lag_min": p.lag_min,
                "n_a": p.n_a, "n_k": p.n_k, "n_lag": p.n_lag}
            for z, p in self.params.items()
        }

    def load(self, data) -> None:
        if not isinstance(data, dict):
            return
        for z, d in data.items():
            try:
                self.params[str(z)] = WinterParams(
                    a=_clamp(float(d["a"]), BOUNDS_A),
                    k_h=_clamp(float(d["k_h"]), BOUNDS_K),
                    lag_min=_clamp(float(d["lag_min"]), BOUNDS_LAG),
                    n_a=int(d.get("n_a", 0)), n_k=int(d.get("n_k", 0)),
                    n_lag=int(d.get("n_lag", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue

    # -- learning --------------------------------------------------------------
    def observe(
        self, zone: str, *, now: datetime, temp: float | None,
        outdoor: float | None, valve: bool | None, solar: float | None,
    ) -> None:
        if temp is None or outdoor is None or valve is None:
            self._seg.pop(zone, None)   # a gap breaks the window
            return
        last = self._last_sample.get(zone)
        seg = self._seg.get(zone)
        if seg is not None and last is not None and now - last > 3 * SAMPLE_EVERY:
            seg = None                  # stale: restart
        if seg is None or seg.valve != valve:
            seg = _Segment(valve=valve, start=now)
            self._seg[zone] = seg
        if last is not None and now - last < SAMPLE_EVERY and seg.samples:
            return
        self._last_sample[zone] = now
        sun = solar if solar is not None else 0.0
        seg.samples.append((now, temp, outdoor, sun))
        cutoff = now - MAX_SEGMENT
        seg.samples = [s for s in seg.samples if s[0] >= cutoff]
        if valve:
            self._learn_lag(zone, seg, now, temp)
        self._learn_window(zone, seg, now)

    def _learn_lag(self, zone: str, seg: _Segment, now: datetime, temp: float) -> None:
        """Dead time by back-extrapolating the measured climb: t1 = first +RISE
        above the post-opening minimum, t2 = first +2·RISE; the climb from the
        minimum took (t2 − t1), so the rise began at t1 − (t2 − t1)."""
        if seg.lag_done:
            return
        if now - seg.start > MAX_LAG_WAIT:
            seg.lag_done = True
            return
        if seg.rise1_at is None:
            seg.min_temp = temp if seg.min_temp is None else min(seg.min_temp, temp)
        rise = temp - seg.min_temp
        if seg.rise1_at is None:
            if rise >= RISE_DETECT:
                seg.rise1_at = now
            return
        if rise < 2 * RISE_DETECT:
            return
        seg.lag_done = True
        t1 = (seg.rise1_at - seg.start).total_seconds() / 60.0
        t2 = (now - seg.start).total_seconds() / 60.0
        lag = _clamp(t1 - (t2 - t1), BOUNDS_LAG)
        p = self.get(zone)
        mean, n = _running(p.lag_min if p.n_lag else lag, p.n_lag, lag)
        self.params[zone] = replace(p, lag_min=mean, n_lag=n)

    def _learn_window(self, zone: str, seg: _Segment, now: datetime) -> None:
        p = self.get(zone)
        settle = RESIDUAL if not seg.valve else timedelta(minutes=p.lag_min)
        begin = max(seg.start + settle, seg.used_until or seg.start)
        window = [s for s in seg.samples if s[0] >= begin]
        if not window or window[-1][0] - window[0][0] < MIN_WINDOW:
            return
        if any(s[3] > LOW_SUN for s in window):
            seg.used_until = now        # sunny window: skip it, don't reuse
            return
        slope = _slope_per_h(window)
        seg.used_until = now
        if slope is None:
            return
        mean_t = sum(s[1] for s in window) / len(window)
        mean_o = sum(s[2] for s in window) / len(window)
        dt = mean_t - mean_o
        if not seg.valve:
            if dt < MIN_DELTA_T or slope >= 0:
                return
            a_obs = _clamp(-slope / dt, BOUNDS_A)
            mean, n = _running(p.a if p.n_a else a_obs, p.n_a, a_obs)
            self.params[zone] = replace(p, a=mean, n_a=n)
        else:
            k_obs = _clamp(slope + p.a * dt, BOUNDS_K)
            mean, n = _running(p.k_h if p.n_k else k_obs, p.n_k, k_obs)
            self.params[zone] = replace(p, k_h=mean, n_k=n)
