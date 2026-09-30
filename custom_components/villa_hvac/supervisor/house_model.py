"""House-level cooling runtime model (v0.73.0, system review 2026-09-30 §3).

The per-room grey-box RLS struggles to separate its terms (collinear a/c over a
daily cycle, unmeasured occupants/doors/windows). The quantity #9/#7 actually
plan against is house-level and well identified: compressor run-hours per day
versus the weather that day.

    runtime_h ≈ β0 + β1 · CDH + β2 · solar_kwh

CDH = cooling degree-hours above CDH_BASE (Σ max(0, T_out − base)·dt), solar in
kWh/m² (Σ GHI·dt). Fitted by plain least squares over recent, fully observed,
occupied summer days. A day right after an absence (Via/Vacanza) is a RECOVERY
day: the envelope's thermal mass was charged with heat while the house coasted,
so it runs longer than its weather explains (live 2026: 9/8, 20/8, 30/8, 9/9 —
the last one 23 h on a 27 °C day). Recovery days are kept out of the fit and
their excess over the prediction is reported: it is the price of the setback.

Import-pure.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

CDH_BASE = 24.0                 # °C: outdoor temp above which the envelope gains
MIN_COVERAGE = 0.9              # fraction of the day observed to accept a row
MIN_FIT_DAYS = 8
MAX_ROWS = 120
MAX_GAP_S = 90.0                # do not credit a longer unobserved interval


@dataclass
class DayAcc:
    day: str                    # local ISO date
    runtime_h: float = 0.0
    cdh: float = 0.0
    solar_kwh: float = 0.0
    observed_s: float = 0.0
    absent: bool = False        # any Via / Vacanza this day
    all_summer: bool = True


@dataclass(frozen=True)
class DayRow:
    day: str
    runtime_h: float
    cdh: float
    solar_kwh: float
    coverage: float
    absent: bool
    recovery: bool              # first present day after an absent day


@dataclass(frozen=True)
class RuntimeFit:
    beta: tuple[float, float, float]
    r2: float
    n: int

    def predict(self, cdh: float, solar_kwh: float, day_fraction: float = 1.0) -> float:
        b0, b1, b2 = self.beta
        return max(0.0, b0 * day_fraction + b1 * cdh + b2 * solar_kwh)


def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a 3x3 system."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(3):
            if r != col:
                f = a[r][col] / a[col][col]
                for c in range(col, 4):
                    a[r][c] -= f * a[col][c]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit_runtime(rows: list[DayRow]) -> RuntimeFit | None:
    """OLS over the fit-eligible rows (present, non-recovery, covered)."""
    data = [r for r in rows if not r.absent and not r.recovery]
    if len(data) < MIN_FIT_DAYS:
        return None
    xs = [(1.0, r.cdh, r.solar_kwh) for r in data]
    ys = [r.runtime_h for r in data]
    xtx = [[sum(x[i] * x[j] for x in xs) for j in range(3)] for i in range(3)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(3)]
    beta = _solve3(xtx, xty)
    if beta is None:
        return None
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum(
        (y - (beta[0] + beta[1] * x[1] + beta[2] * x[2])) ** 2 for x, y in zip(xs, ys)
    )
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return RuntimeFit(beta=(beta[0], beta[1], beta[2]), r2=r2, n=len(data))


class HouseRuntimeModel:
    """Accumulates one DayAcc per local day and closes it into a DayRow."""

    def __init__(self) -> None:
        self.rows: list[DayRow] = []
        self.today: DayAcc | None = None
        self._last_s: float | None = None

    def observe(
        self, *, now_s: float, local_day: str, consenso_on: bool | None,
        outdoor: float | None, ghi: float | None, absent: bool, summer: bool,
    ) -> None:
        if self.today is None or self.today.day != local_day:
            if self.today is not None:
                self._close(self.today)
            self.today = DayAcc(day=local_day)
            self._last_s = None
        acc = self.today
        acc.absent = acc.absent or absent
        acc.all_summer = acc.all_summer and summer
        if self._last_s is not None:
            dt = now_s - self._last_s
            if 0 < dt <= MAX_GAP_S and consenso_on is not None:
                acc.observed_s += dt
                h = dt / 3600.0
                if consenso_on:
                    acc.runtime_h += h
                if outdoor is not None:
                    acc.cdh += max(0.0, outdoor - CDH_BASE) * h
                if ghi is not None and ghi > 0:
                    acc.solar_kwh += ghi * h / 1000.0
        self._last_s = now_s

    def _close(self, acc: DayAcc) -> None:
        coverage = acc.observed_s / 86400.0
        if not acc.all_summer or coverage < MIN_COVERAGE:
            # Not a usable row, but an absence still marks tomorrow as recovery.
            if acc.absent:
                self.rows.append(DayRow(acc.day, acc.runtime_h, acc.cdh,
                                        acc.solar_kwh, coverage, True, False))
                self.rows = self.rows[-MAX_ROWS:]
            return
        prev = self.rows[-1] if self.rows else None
        recovery = (not acc.absent) and prev is not None and prev.absent
        self.rows.append(DayRow(
            acc.day, round(acc.runtime_h, 3), round(acc.cdh, 2),
            round(acc.solar_kwh, 3), round(coverage, 3), acc.absent, recovery,
        ))
        self.rows = self.rows[-MAX_ROWS:]

    def fit(self) -> RuntimeFit | None:
        return fit_runtime(self.rows)

    def recovery_excess(self, fit: RuntimeFit | None) -> float | None:
        """Mean (actual − predicted) run-hours over the recovery days."""
        if fit is None:
            return None
        rec = [r for r in self.rows if r.recovery]
        if not rec:
            return None
        return sum(r.runtime_h - fit.predict(r.cdh, r.solar_kwh) for r in rec) / len(rec)

    # -- persistence -----------------------------------------------------------
    def dump(self) -> dict:
        return {
            "rows": [asdict(r) for r in self.rows],
            "today": asdict(self.today) if self.today is not None else None,
        }

    def load(self, data: dict | None) -> None:
        if not data:
            return
        rows: list[DayRow] = []
        for d in data.get("rows") or []:
            try:
                rows.append(DayRow(
                    str(d["day"]), float(d["runtime_h"]), float(d["cdh"]),
                    float(d["solar_kwh"]), float(d["coverage"]),
                    bool(d["absent"]), bool(d["recovery"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        self.rows = rows[-MAX_ROWS:]
        t = data.get("today")
        if isinstance(t, dict):
            try:
                self.today = DayAcc(
                    day=str(t["day"]), runtime_h=float(t["runtime_h"]),
                    cdh=float(t["cdh"]), solar_kwh=float(t["solar_kwh"]),
                    observed_s=float(t["observed_s"]), absent=bool(t["absent"]),
                    all_summer=bool(t["all_summer"]),
                )
            except (KeyError, TypeError, ValueError):
                self.today = None
