"""Tests for the #6 cooling-compressor run-time KPI accumulator."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac.const import DOMAIN
from custom_components.villa_hvac.coordinator import VillaHvacCoordinator, runtime_step

POLL = 30.0


def _step(**kw):
    base = dict(
        now_s=0.0, last_ts_s=None, last_consenso=None, consenso=None,
        runtime_s=0.0, cycles=0, poll_s=POLL,
    )
    base.update(kw)
    return runtime_step(**base)


# --- pure accumulation -------------------------------------------------------

def test_first_sample_no_credit_no_cycle():
    assert _step(now_s=100.0, consenso="on") == (0.0, 0)


def test_contiguous_on_credits_delta():
    assert _step(now_s=130.0, last_ts_s=100.0, last_consenso="on",
                 consenso="on") == (30.0, 0)


def test_off_window_credits_nothing():
    assert _step(now_s=130.0, last_ts_s=100.0, last_consenso="off",
                 consenso="off") == (0.0, 0)


def test_off_to_on_counts_a_start_but_no_runtime():
    rt, cy = _step(now_s=130.0, last_ts_s=100.0, last_consenso="off", consenso="on")
    assert cy == 1 and rt == 0.0  # the window just ended was off


def test_gap_longer_than_cap_not_credited():
    # last seen 900 s ago (> 3x poll) -> restart/outage gap, don't credit as run-time
    assert _step(now_s=1000.0, last_ts_s=100.0, last_consenso="on",
                 consenso="on") == (0.0, 0)


def test_cap_boundary_inclusive():
    assert _step(now_s=90.0, last_ts_s=0.0, last_consenso="on",
                 consenso="on")[0] == 90.0
    assert _step(now_s=91.0, last_ts_s=0.0, last_consenso="on",
                 consenso="on")[0] == 0.0


def test_run_accumulates_across_steps_one_start():
    rt, cy = 0.0, 0
    rt, cy = _step(now_s=30, last_ts_s=0, last_consenso="off", consenso="on",
                   runtime_s=rt, cycles=cy)   # start
    rt, cy = _step(now_s=60, last_ts_s=30, last_consenso="on", consenso="on",
                   runtime_s=rt, cycles=cy)    # +30
    rt, cy = _step(now_s=90, last_ts_s=60, last_consenso="on", consenso="on",
                   runtime_s=rt, cycles=cy)    # +30
    assert rt == 60.0 and cy == 1


def test_unknown_breaks_run_without_a_phantom_start():
    rt, cy = _step(now_s=130, last_ts_s=100, last_consenso="on", consenso=None)
    assert rt == 30.0 and cy == 0            # was on across the window -> credited
    _, cy2 = _step(now_s=160, last_ts_s=130, last_consenso=None, consenso="on")
    assert cy2 == 0                          # unknown->on is not a counted start


# --- restore seeding ---------------------------------------------------------

async def test_seed_runtime_base_is_monotonic(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    coord = VillaHvacCoordinator(hass, entry)
    assert coord.cool_runtime_hours == 0.0
    coord.seed_runtime_base(2.0)               # restored from a prior run
    assert coord.cool_runtime_hours == 2.0
    coord.cool_runtime_s = 3600.0              # +1 h this run
    assert coord.cool_runtime_hours == 3.0
    coord.seed_runtime_base(-5.0)              # garbage restore ignored
    assert coord.cool_runtime_hours == 3.0
