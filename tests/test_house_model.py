"""v0.73.0 house-level runtime model (run-hours/day vs CDH + solar)."""
from __future__ import annotations

from custom_components.villa_hvac.supervisor.house_model import (
    CDH_BASE,
    DayRow,
    HouseRuntimeModel,
    fit_runtime,
)


def _row(day, cdh, solar, runtime, *, absent=False, recovery=False):
    return DayRow(day, runtime, cdh, solar, 1.0, absent, recovery)


def test_fit_recovers_known_coefficients():
    rows = [
        _row(f"d{i}", cdh, sol, 1.0 + 0.1 * cdh + 0.5 * sol)
        for i, (cdh, sol) in enumerate(
            [(10, 5), (40, 6), (80, 7), (20, 3), (60, 8), (30, 4), (90, 6), (5, 2)]
        )
    ]
    fit = fit_runtime(rows)
    assert fit is not None and fit.n == 8
    b0, b1, b2 = fit.beta
    assert abs(b0 - 1.0) < 1e-6 and abs(b1 - 0.1) < 1e-6 and abs(b2 - 0.5) < 1e-6
    assert fit.r2 > 0.999


def test_recovery_and_absent_days_are_kept_out_of_the_fit():
    base = [_row(f"d{i}", 10 * i, 3 + i % 3, 2 + 0.1 * 10 * i) for i in range(1, 9)]
    rows = base + [
        _row("away", 80, 6, 0.0, absent=True),
        _row("back", 30, 5, 23.0, recovery=True),    # the 9/9-like day
    ]
    fit = fit_runtime(rows)
    assert fit.n == 8
    m = HouseRuntimeModel()
    m.rows = rows
    assert m.recovery_excess(fit) > 15.0             # 23 h vs ~5 h predicted


def test_too_few_days_no_fit():
    assert fit_runtime([_row("d", 10, 5, 3)] * 5) is None


def test_accumulation_and_day_close_marks_recovery():
    m = HouseRuntimeModel()
    t = 0.0
    # day 1: absent all day, compressor off, outdoor 34
    for _ in range(2880):
        m.observe(now_s=t, local_day="2026-09-08", consenso_on=False,
                  outdoor=34.0, ghi=0.0, absent=True, summer=True)
        t += 30
    # day 2: present, compressor on all day, outdoor 26, sun 500 W/m²
    for _ in range(2880):
        m.observe(now_s=t, local_day="2026-09-09", consenso_on=True,
                  outdoor=26.0, ghi=500.0, absent=False, summer=True)
        t += 30
    m.observe(now_s=t, local_day="2026-09-10", consenso_on=False,
              outdoor=20.0, ghi=0.0, absent=False, summer=True)
    away, back = m.rows
    assert away.absent and not away.recovery
    assert back.recovery and not back.absent
    assert abs(back.runtime_h - 24.0) < 0.05
    assert abs(back.cdh - (26.0 - CDH_BASE) * 24.0) < 0.1
    assert abs(back.solar_kwh - 12.0) < 0.05


def test_unobserved_gap_is_not_credited_and_low_coverage_day_dropped():
    m = HouseRuntimeModel()
    m.observe(now_s=0, local_day="d1", consenso_on=True, outdoor=30.0, ghi=0.0,
              absent=False, summer=True)
    m.observe(now_s=3600, local_day="d1", consenso_on=True, outdoor=30.0, ghi=0.0,
              absent=False, summer=True)
    assert m.today.runtime_h == 0.0
    m.observe(now_s=3630, local_day="d2", consenso_on=True, outdoor=30.0, ghi=0.0,
              absent=False, summer=True)
    assert m.rows == []                               # d1 coverage << 90 %


def test_dump_load_roundtrip():
    m = HouseRuntimeModel()
    m.rows = [_row("d1", 10, 5, 3.0), _row("d2", 20, 6, 4.0, recovery=True)]
    m.observe(now_s=0, local_day="d3", consenso_on=True, outdoor=30.0, ghi=100.0,
              absent=False, summer=True)
    m2 = HouseRuntimeModel()
    m2.load(m.dump())
    assert m2.rows == m.rows
    assert m2.today.day == "d3"
    m2.load({"rows": [{"bad": 1}]})
    assert m2.rows == []
