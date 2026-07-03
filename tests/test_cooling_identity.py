"""Tier-1 P2: the differential identity harness (STORY §2 P2 gate a/c/d/e/f).

Runs the OLD pipeline — the real DutyController + FanBandController +
RegimeCoordinator wired EXACTLY as engine.py:765-778 wired them at v0.39.0
(regime step → prepend regime BLOCCO → duty → band(phase_override) →
merge_desired) — against the folded `CoolingController()(state)`, asserting per
cycle over multi-cycle scripted sequences:

  * identical merged Desired dicts AND identical key ORDER (the engine writes
    levers in dict order — the stream must stay byte-identical);
  * the returned Desired ALWAYS contains BLOCCO_LEVER (invariant-2 property);
  * identical post-state (_duty, _rs, _states, _last_fan).

The trio stays in the repo UNWIRED through the v0.40.0 soak as this oracle;
this file's old-pipeline arm is deleted at P3.

ALLOWLISTED DEVIATIONS (both applied to BOTH arms, so the differential
isolates the FOLD as the only thing under test):
  1. (STORY §4 risk 4 / §5.6) gate reads are snapshot-consistent
     (state.duty_enabled / state.fan_pacing_enabled); the old engine re-read
     the live switches mid-cycle. Pinned by the dedicated
     test_gate_reads_are_snapshot_consistent (mid-cycle switch flip: the fold
     follows the snapshot; the old code would have reset).
  2. regime_pass guards `state.config is None` as gate-off; the old
     engine._regime_step would have raised on a config-less state.
     Production-unreachable (build_house_state always attaches config).

ORACLE HONESTY (review 2026-07-03): OldPipeline's ~50 wiring lines are a
same-author transliteration of the old engine code — a shared misreading would
pass both it and the fold. The transliteration-bias-FREE evidence for the
identity claim is: (a) the trio classes inside OldPipeline are the REAL,
byte-identical v0.38/v0.39 classes; (b) the entire 526-test pre-fold suite —
including the three seam tests — passes UNCHANGED through the swapped engine.
The test_ab_* engine A/B below additionally pins engine-level equal ORDERED
service-call streams between the two wirings (both run through the new
engine's _cycle; it does not re-create the deleted v0.39 _cycle).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import itertools

import pytest

from custom_components.villa_hvac.const import (
    OPT_DUTY_COOLOFF,
    OPT_DUTY_MAX_STINT,
    OPT_REGIME_ENABLED,
    SCHEDULE_MAX_AGE,
    SEASON_SUMMER,
)
from custom_components.villa_hvac.policies import (
    CoolingController,
    DutyController,
    FanBandController,
    RegimeCoordinator,
)
from custom_components.villa_hvac.supervisor import (
    BLOCCO_BLOCK,
    BLOCCO_LEVER,
    BLOCCO_RELEASE,
    DutyState,
    HouseState,
    RegimeState,
    ZoneSnapshot,
    annotate_centers,
    fan_lever,
    house_load_index,
    merge_desired,
    select_regime,
    switch_lever,
    temperature_lever,
)
from custom_components.villa_hvac.supervisor_config import SupervisorConfig

# priors used by the regime classification (mirror const.COOL_*)
from custom_components.villa_hvac.const import (  # noqa: E402
    COOL_CAPACITY,
    COOL_GAIN_BASE,
    COOL_GAIN_OUTDOOR,
    COOL_GAIN_SOLAR,
    REGIME_K_CONF_MIN,
)

T0 = datetime(2026, 7, 3, 12, 0, 0)


# --- the OLD pipeline (the identity oracle; deleted at P3) ---------------------


class OldPipeline:
    """The v0.39.0 composition: the REAL trio classes, wired exactly as
    engine.py:765-778 + engine._regime_step:955-993 wired them. Kept textually
    faithful to that engine code (only the gate reads use the state snapshot —
    the allowlisted deviation, applied to both arms)."""

    def __init__(self) -> None:
        self.duty = DutyController()
        self.band = FanBandController()
        self.regime = RegimeCoordinator()

    def _regime_step(self, state: HouseState):
        # = engine._regime_step (v0.39.0) with snapshot gate reads.
        cfg = state.config
        coalescing = (
            cfg is not None
            and cfg.regime_enabled
            and state.duty_enabled
            and state.fan_pacing_enabled
        )
        if not coalescing:
            return self.regime.step(
                state, regime="low", center=None, min_on=None, min_off=None
            )
        load = house_load_index(
            state, default_a=COOL_GAIN_OUTDOOR, default_b=COOL_GAIN_SOLAR,
            default_c=COOL_GAIN_BASE, default_capacity=COOL_CAPACITY,
            k_conf_min=REGIME_K_CONF_MIN,
        )
        at_peak = (
            state.outdoor_temp is not None and state.duty_peak_outdoor is not None
            and state.outdoor_temp >= state.duty_peak_outdoor
        )
        free_cool = (
            state.free_cool_enabled and state.season == SEASON_SUMMER
            and state.outdoor_temp is not None
            and state.free_cool_threshold is not None
            and state.outdoor_temp < state.free_cool_threshold
        )
        regime = select_regime(
            load, at_peak=at_peak, free_cool=free_cool,
            peak_ratio=cfg.regime_peak_ratio, medium_ratio=cfg.regime_medium_ratio,
        )
        center = (
            state.house_setpoint + state.mode_offset
            if (state.house_setpoint is not None and state.mode_offset is not None)
            else None
        )
        return self.regime.step(
            state, regime=regime, center=center,
            min_on=cfg.min_compressor_on, min_off=cfg.min_compressor_off,
        )

    def __call__(self, state: HouseState) -> dict:
        # = the engine's actuate block (v0.39.0 engine.py:765-778), controllers only.
        phase_override, regime_blocco = self._regime_step(state)
        ctrl_outputs: list = []
        if regime_blocco is not None:
            ctrl_outputs.append({BLOCCO_LEVER: regime_blocco})
        ctrl_outputs.append(self.duty(state))
        ctrl_outputs.append(self.band(state, phase_override=phase_override))
        return merge_desired(ctrl_outputs)


def _post_old(p: OldPipeline):
    return (p.duty._duty, p.regime._rs, dict(p.band._states), dict(p.band._last_fan))


def _post_new(c: CoolingController):
    return (c._duty, c._rs, dict(c._states), dict(c._last_fan))


def _run_differential(states, *, old=None, new=None):
    """Feed the same state sequence through both pipelines; assert per cycle:
    identical Desired (values AND key order), BLOCCO always present, identical
    post-state. Returns (old, new) for further pinning."""
    old = old or OldPipeline()
    new = new or CoolingController()
    for i, state in enumerate(states):
        out_old = old(state)
        out_new = new(state)
        assert out_new == out_old, f"cycle {i}: Desired diverged\n{out_new}\n{out_old}"
        assert list(out_new.keys()) == list(out_old.keys()), (
            f"cycle {i}: lever write ORDER diverged"
        )
        assert BLOCCO_LEVER in out_new, f"cycle {i}: no BLOCCO opinion"
        assert _post_new(new) == _post_old(old), f"cycle {i}: post-state diverged"
    return old, new


# --- state construction --------------------------------------------------------


def _cfg(*, regime=False, stint_min=120, cooloff_min=30) -> SupervisorConfig:
    return SupervisorConfig.from_options({
        OPT_REGIME_ENABLED: regime,
        OPT_DUTY_MAX_STINT: stint_min,
        OPT_DUTY_COOLOFF: cooloff_min,
    })


def _leader(zid, *, temp, bedroom=False, converged=True, follows=None, climate=True):
    return ZoneSnapshot(
        zone_id=zid, name=zid,
        climate=f"climate.{zid}" if climate else None,
        emitter="fancoil", temp=temp, bedroom=bedroom, follows=follows,
        fancoil_units=((f"fan.{zid}", f"switch.{zid}_man"),),
        model_a=0.03 if converged else None,
        model_b=0.0008 if converged else None,
        model_c=0.0 if converged else None,
        model_k=1.2 if converged else None,
        model_k_confidence=0.9 if converged else None,
    )


def _hs(
    temps: dict[str, float], *, now=T0, cfg=None,
    fan_pacing=True, duty=True, season=SEASON_SUMMER,
    free_cool=False, night=False, consenso="on", at_peak=False, precool=False,
    paused=(), solar=100.0, annotate=True,
):
    """One harness HouseState: leaders lr/sg + bedroom bed + follower kit, with
    the config-derived scalar fields mirroring build_house_state, ANNOTATED
    (production parity — the band reads z.resolved_center)."""
    cfg = cfg or _cfg()
    zones = {}
    for zid, t in temps.items():
        z = _leader(zid, temp=t, bedroom=(zid == "bed"))
        if zid in paused:
            z = replace(z, paused=True)
        zones[zid] = z
    zones["kit"] = _leader("kit", temp=25.0, follows="lr", climate=False)
    outdoor = 20.0 if free_cool else (34.0 if at_peak else 26.0)
    state = HouseState(
        now=now, zones=zones, season=season, house_mode="Casa",
        house_setpoint=24.0, mode_offset=0.0,
        band_width=cfg.band_width, band_slam=cfg.band_slam,
        duty_enabled=duty, fan_pacing_enabled=fan_pacing,
        duty_max_stint=cfg.duty_max_stint, duty_cooloff=cfg.duty_cooloff,
        duty_comfort_max=cfg.duty_comfort_max,
        duty_peak_outdoor=cfg.duty_peak_outdoor,
        comfort_floor=22.0,
        free_cool_enabled=free_cool, free_cool_threshold=cfg.free_cool_outdoor,
        night_active=night, consenso_freddo=consenso,
        precool=precool, precool_offset=cfg.precool_offset,
        outdoor_temp=outdoor, solar=solar, config=cfg,
    )
    return annotate_centers(state, max_age=SCHEDULE_MAX_AGE) if annotate else state


# temps straddling center(24) ± B/2(0.75) ± ε — RUN / hold / REST / re-RUN / hold
_LR_SCRIPT = (26.0, 24.7, 23.2, 24.8, 23.9)
_SG_SCRIPT = (25.0, 24.0, 23.24, 25.76, 24.0)
_BED_SCRIPT = (25.5, 24.74, 23.2, 24.76, 24.5)


# --- (a) the gate lattice ------------------------------------------------------

_LATTICE = list(itertools.product(
    (False, True),            # fan_pacing
    (False, True),            # duty
    (False, True),            # regime_enabled
    (SEASON_SUMMER, "winter"),  # season
    (False, True),            # free_cool
    (False, True),            # night_active (bedroom owned by #2b)
    ("on", "off", "unavailable"),  # consenso
    (False, True),            # at_peak
    (False, True),            # precool
))


@pytest.mark.parametrize(
    "fan_pacing, duty, regime, season, free_cool, night, consenso, at_peak, precool",
    _LATTICE,
)
def test_lattice_differential(
    fan_pacing, duty, regime, season, free_cool, night, consenso, at_peak, precool
):
    cfg = _cfg(regime=regime)
    states = [
        _hs(
            {"lr": _LR_SCRIPT[i], "sg": _SG_SCRIPT[i], "bed": _BED_SCRIPT[i]},
            now=T0 + timedelta(minutes=10 * i), cfg=cfg,
            fan_pacing=fan_pacing, duty=duty, season=season, free_cool=free_cool,
            night=night, consenso=consenso, at_peak=at_peak, precool=precool,
        )
        for i in range(len(_LR_SCRIPT))
    ]
    _run_differential(states)


def test_live_combo_weighted_heaviest():
    """The LIVE combination (duty+fan ON, regime OFF) over a denser script:
    every consenso variant, precool and at-peak transitions, temps walking the
    full band — the path that is actually running in the villa today."""
    cfg = _cfg()
    lr = (26.0, 24.8, 24.4, 23.2, 23.2, 24.76, 26.5, 24.0, 23.24, 25.0)
    sg = (25.0, 24.7, 24.7, 23.9, 23.1, 24.0, 27.5, 24.5, 23.2, 24.9)
    consensi = ("on", "on", "unavailable", "on", "off", "on", "on", "unknown", "on", "on")
    peaks = (False, False, False, False, False, False, True, True, False, False)
    precools = (False, True, True, False, False, False, False, False, False, True)
    states = [
        _hs(
            {"lr": lr[i], "sg": sg[i], "bed": 24.5},
            now=T0 + timedelta(minutes=10 * i), cfg=cfg,
            consenso=consensi[i], at_peak=peaks[i], precool=precools[i],
        )
        for i in range(len(lr))
    ]
    _run_differential(states)


# --- (a) the four MANDATORY sequences (adversarial verdicts) -------------------


def test_seq1_night_toggle_hysteresis_adjacent_fan():
    """(1) RUN establishes _last_fan=L for the bedroom → N night cycles (#2b owns
    it) → wake with the raw load INSIDE L's hysteresis window ⇒ fan == L in both
    pipelines AND the _last_fan key SURVIVED the night (the load-bearing
    asymmetry: the night pop clears _states only). Drift here shifts the morning
    fan a level AND fragments F2b k-windows."""
    cfg = _cfg()
    # bed 24.3 (just above center 24 -> RUN): load = 0.03*(26-24.3) + 0.0008*100
    # = 0.131; effective pulldown = 0.3 + (24.3-24)/2 = 0.45; raw = 100*(0.131+
    # 0.45)/1.2 = 48.4 -> level 50 (2026-07-04 sizing law).
    s_run = _hs({"lr": 25.0, "sg": 24.0, "bed": 24.3}, cfg=cfg)
    old, new = _run_differential([s_run])
    fan_l = new._last_fan["bed"]
    assert fan_l == 50

    night_states = [
        _hs({"lr": 25.0, "sg": 24.0, "bed": 25.0},
            now=T0 + timedelta(minutes=10 * (i + 1)), cfg=cfg, night=True)
        for i in range(3)
    ]
    _run_differential(night_states, old=old, new=new)
    assert "bed" in new._last_fan and new._last_fan["bed"] == fan_l  # survived
    assert "bed" in old.band._last_fan  # the oracle agrees it's an asymmetry

    # wake: bed 24.2 → load = 0.03*1.8+0.08 = 0.134; pull = 0.3+0.1 = 0.4;
    # raw = 100*0.534/1.2 = 44.5 (level 40 from scratch) but |44.5-50| < 10
    # (step/2 + hysteresis) → HELD at 50 iff _last_fan survived the night.
    s_wake = _hs({"lr": 25.0, "sg": 24.0, "bed": 24.2},
                 now=T0 + timedelta(minutes=40), cfg=cfg, night=False)
    out_old = old(s_wake)
    out_new = new(s_wake)
    assert out_new == out_old
    assert out_new[fan_lever("fan.bed")] == fan_l  # 50, not the from-scratch 40
    assert _post_new(new) == _post_old(old)


def test_seq2_released_then_disable_then_reenable():
    """(2) eligible RUN → paused (released BandState stored; manuale-off
    re-emitted EVERY cycle) → fan_pacing off (_release_all: offs once, dicts
    cleared; then {}) → re-enable (fresh take)."""
    cfg = _cfg()
    run = _hs({"lr": 26.0}, cfg=cfg)
    old, new = _run_differential([run])
    assert new._states["lr"].phase == "run"

    paused = [
        _hs({"lr": 26.0}, now=T0 + timedelta(minutes=10 * (i + 1)), cfg=cfg,
            paused=("lr",))
        for i in range(2)
    ]
    for i, s in enumerate(paused):
        out_old, out_new = old(s), new(s)
        assert out_new == out_old
        # the released-zone re-emission asymmetry: manuale-off EVERY cycle.
        assert out_new[switch_lever("switch.lr_man")] == "off", f"paused cycle {i}"
        assert temperature_lever("climate.lr") not in out_new
        assert _post_new(new) == _post_old(old)
    assert new._states["lr"].phase == "released"  # bookkeeping kept, not dropped

    off1 = _hs({"lr": 26.0}, now=T0 + timedelta(minutes=30), cfg=cfg,
               fan_pacing=False)
    out_old, out_new = old(off1), new(off1)
    assert out_new == out_old
    assert out_new[switch_lever("switch.lr_man")] == "off"  # _release_all one-shot
    assert new._states == {} and new._last_fan == {}

    off2 = _hs({"lr": 26.0}, now=T0 + timedelta(minutes=40), cfg=cfg,
               fan_pacing=False)
    out_old, out_new = old(off2), new(off2)
    assert out_new == out_old
    assert out_new == {BLOCCO_LEVER: BLOCCO_RELEASE}  # band contributes {} now

    back = _hs({"lr": 26.0}, now=T0 + timedelta(minutes=50), cfg=cfg)
    out_old, out_new = old(back), new(back)
    assert out_new == out_old
    assert out_new[temperature_lever("climate.lr")] == 23.25  # fresh RUN take
    assert _post_new(new) == _post_old(old)


def test_seq3_season_flap():
    """(3) summer → winter → summer: the winter flip releases everything
    (manuale offs once, then {}), the summer return re-takes fresh."""
    cfg = _cfg()
    seq = [
        _hs({"lr": 26.0, "sg": 25.0, "bed": 25.5}, cfg=cfg),
        _hs({"lr": 26.0, "sg": 25.0, "bed": 25.5},
            now=T0 + timedelta(minutes=10), cfg=cfg, season="winter"),
        _hs({"lr": 26.0, "sg": 25.0, "bed": 25.5},
            now=T0 + timedelta(minutes=20), cfg=cfg, season="winter"),
        _hs({"lr": 26.0, "sg": 25.0, "bed": 25.5},
            now=T0 + timedelta(minutes=30), cfg=cfg),
    ]
    old, new = _run_differential(seq)
    assert new._states["lr"].phase == "run"  # re-taken after the flap


def test_seq4_failsafe_handback_then_resume():
    """(4) seed BOTH pipelines mid-cooloff; the fail-safe releases BLOCCO
    externally (controller state untouched) and cycles resume ⇒ identical BLOCCO
    opinions until cooloff_until elapses, then RELEASE — never a lingering
    block."""
    cfg = _cfg(cooloff_min=20)
    old, new = OldPipeline(), CoolingController()
    seed = DutyState(cooloff_until=T0 + timedelta(minutes=20))
    old.duty._duty = seed
    new._duty = seed

    expected = (BLOCCO_BLOCK, BLOCCO_BLOCK, BLOCCO_RELEASE, BLOCCO_RELEASE)
    for i, minutes in enumerate((5, 15, 25, 35)):
        s = _hs({"lr": 25.0, "sg": 24.0, "bed": 24.5},
                now=T0 + timedelta(minutes=minutes), cfg=cfg, consenso="off")
        out_old, out_new = old(s), new(s)
        assert out_new == out_old, f"resume cycle {i}"
        assert out_new[BLOCCO_LEVER] == expected[i], f"resume cycle {i}"
        assert _post_new(new) == _post_old(old)
    assert new._duty.cooloff_until is None  # never a lingering block


# --- duty timer expiries + breach abort (stint/cooloff lattice arm) ------------


def test_stint_expiry_then_cooloff_then_release():
    cfg = _cfg(stint_min=60, cooloff_min=30)
    temps = {"lr": 25.0, "sg": 24.0, "bed": 24.5}
    minutes = (0, 30, 70, 90, 105)
    # 0/30: within stint → RELEASE; 70: stint(60) exceeded → BLOCK (cooloff to
    # 100); 90: still blocking; 105: cooloff elapsed → RELEASE.
    expected = (
        BLOCCO_RELEASE, BLOCCO_RELEASE, BLOCCO_BLOCK, BLOCCO_BLOCK, BLOCCO_RELEASE
    )
    old, new = OldPipeline(), CoolingController()
    for i, m in enumerate(minutes):
        s = _hs(temps, now=T0 + timedelta(minutes=m), cfg=cfg)
        out_old, out_new = old(s), new(s)
        assert out_new == out_old, f"cycle {i}"
        assert out_new[BLOCCO_LEVER] == expected[i], f"cycle {i}"
        assert _post_new(new) == _post_old(old)


def test_comfort_breach_aborts_cooloff():
    cfg = _cfg(cooloff_min=60)
    old, new = OldPipeline(), CoolingController()
    seed = DutyState(cooloff_until=T0 + timedelta(minutes=60))
    old.duty._duty = seed
    new._duty = seed
    hot = _hs({"lr": 28.0, "sg": 24.0, "bed": 24.5}, cfg=cfg)  # 28 > comfort 27
    out_old, out_new = old(hot), new(hot)
    assert out_new == out_old
    assert out_new[BLOCCO_LEVER] == BLOCCO_RELEASE  # comfort wins over the timer
    assert new._duty == DutyState()
    assert _post_new(new) == _post_old(old)


# --- MEDIUM coalescing differential (the regime path itself) -------------------


def test_medium_coalescing_differential():
    """regime+duty+fan all on, MEDIUM-classifiable load: the coalescing drives a
    synced phase + RELEASE in both pipelines, RUN/REST transitions included."""
    cfg = _cfg(regime=True)
    lr = (25.5, 25.0, 23.1, 23.1, 25.6)
    sg = (25.0, 24.5, 23.2, 23.0, 25.5)
    bed = (24.8, 24.4, 23.0, 23.1, 25.4)
    states = [
        _hs({"lr": lr[i], "sg": sg[i], "bed": bed[i]},
            now=T0 + timedelta(minutes=15 * i), cfg=cfg, solar=200.0)
        for i in range(len(lr))
    ]
    old, new = _run_differential(states)
    assert new._rs == old.regime._rs
    assert new._rs.house_phase in ("run", "rest")  # coalescing actually engaged
    assert new.regime_driving == "medium"


def test_medium_breach_forces_run_differential():
    """Invariant 1 on the regime path (mutation-proven hole from the adversarial
    review): comfort-breach-forces-RUN INSIDE MEDIUM coalescing. The sequence
    pins the breach specifically — at the breach cycle the REST is younger than
    min_off (10 min), so the enter-threshold path is blocked and ONLY the breach
    can force the RUN. A fold typo dropping comfort_breach from the
    coalesce_phase call fails here and nowhere else."""
    cfg = _cfg(regime=True)
    old, new = OldPipeline(), CoolingController()

    # c1: hot -> coalescing RUN (run_started = T0).
    c1 = _hs({"lr": 25.5, "sg": 25.0, "bed": 25.0}, cfg=cfg, solar=200.0)
    # c2 (T0+15, min_on elapsed): all cold -> REST (rest_started = T0+15).
    c2 = _hs({"lr": 23.2, "sg": 23.2, "bed": 23.2},
             now=T0 + timedelta(minutes=15), cfg=cfg, solar=200.0)
    # c3 (T0+20, REST only 5 min old < min_off): lr breaches comfort (28 > 27)
    # -> RUN forced by the BREACH alone (the threshold path is timer-blocked).
    c3 = _hs({"lr": 28.0, "sg": 23.2, "bed": 23.2},
             now=T0 + timedelta(minutes=20), cfg=cfg, solar=200.0)

    for i, s in enumerate((c1, c2, c3)):
        out_old, out_new = old(s), new(s)
        assert out_new == out_old, f"cycle {i}"
        assert _post_new(new) == _post_old(old), f"cycle {i}"
    assert new._rs.house_phase == "run"                    # breach forced RUN
    assert new._duty == DutyState()                        # duty saw the breach too
    assert out_new[BLOCCO_LEVER] == BLOCCO_RELEASE
    assert out_new[temperature_lever("climate.lr")] == 23.25  # RUN slam, not 24.75


def test_medium_release_beats_duty_block_and_duty_still_advances():
    """Timer expiry CROSSED with the regime path: a stint expiring while MEDIUM-
    coalescing — the duty wants BLOCK, the coalescing RELEASE wins the merge,
    AND the duty timers still advanced (duty_pass ALWAYS runs; its cooloff is
    armed even while overridden — exactly the old engine's semantics)."""
    cfg = _cfg(regime=True, stint_min=15, cooloff_min=30)
    old, new = OldPipeline(), CoolingController()
    seed = DutyState(stint_start=T0 - timedelta(minutes=20))  # stint exceeded
    old.duty._duty = seed
    new._duty = seed

    s = _hs({"lr": 25.5, "sg": 25.0, "bed": 25.0}, cfg=cfg, solar=200.0)
    out_old, out_new = old(s), new(s)

    assert out_new == out_old
    assert out_new[BLOCCO_LEVER] == BLOCCO_RELEASE     # coalescing RELEASE won
    assert new._duty.cooloff_until is not None         # ...but duty ADVANCED
    assert new._rs.house_phase == "run"
    assert _post_new(new) == _post_old(old)


def test_free_cool_enabled_but_inactive_differential():
    """Lattice de-aliasing (review finding): free_cool ENABLED with the outdoor
    ABOVE the threshold (enabled-but-inactive) never appears in the lattice,
    whose free_cool axis couples enablement with a 20 °C outdoor. Cover it."""
    cfg = _cfg()
    states = [
        replace(
            _hs({"lr": _LR_SCRIPT[i], "sg": _SG_SCRIPT[i], "bed": _BED_SCRIPT[i]},
                now=T0 + timedelta(minutes=10 * i), cfg=cfg, annotate=False),
            free_cool_enabled=True,   # enabled...
            outdoor_temp=26.0,        # ...but NOT active (26 > threshold 22)
        )
        for i in range(len(_LR_SCRIPT))
    ]
    states = [annotate_centers(s, max_age=SCHEDULE_MAX_AGE) for s in states]
    _run_differential(states)


async def test_gate_reads_are_snapshot_consistent(hass):
    """THE allowlisted deviation, pinned (STORY §4 risk 4): the fold's regime
    gating reads the SNAPSHOT (state.duty_enabled / state.fan_pacing_enabled),
    not the live switches. Build the state with the gates ON, then flip the live
    switches OFF before the controller runs: the fold still coalesces per the
    snapshot. The old engine._regime_step re-read the live switches mid-cycle
    and would have RESET here — that one-cycle divergence on a mid-cycle flip is
    the entire declared deviation."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.villa_hvac.const import DOMAIN, OPT_REGIME_ENABLED as ORE
    from custom_components.villa_hvac.engine import build_house_state

    hass.states.async_set(
        "climate.salotto_termostato_2", "cool",
        {"preset_mode": "comfort", "temperature": 24.0},
    )
    hass.states.async_set("sensor.clima_salotto", "25.0")
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "26.0")
    hass.states.async_set("sensor.gw3000a_solar_radiation", "200")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={}, options={ORE: True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    # converged k for living_room so the load ratio classifies MEDIUM.
    engine.thermal.load({
        "living_room": {"a": 0.03, "b": 0.0008, "c": 0.0, "k": 1.2, "p": [0.0] * 9,
                        "p_k": 0.0, "n": 100, "n_k": 100, "s_hi": 400.0},
    })
    await entry.runtime_data.async_refresh()
    hass.states.async_set("switch.duty_cycle", "on")
    hass.states.async_set("switch.fan_pacing", "on")

    state = build_house_state(hass, entry, entry.runtime_data)
    assert state.duty_enabled is True and state.fan_pacing_enabled is True

    # the mid-cycle flip: live switches go OFF after the snapshot was built.
    hass.states.async_set("switch.duty_cycle", "off")
    hass.states.async_set("switch.fan_pacing", "off")

    override, blocco = engine._cooling.regime_pass(state)
    assert override.get("living_room") in ("run", "rest")  # coalesced per SNAPSHOT
    assert blocco == BLOCCO_RELEASE
    assert engine._cooling.regime_driving == "medium"


# --- (d) __call__-level BLOCCO pins --------------------------------------------


def test_call_emits_exact_release_pair_when_everything_disabled():
    """duty disabled + regime yielding + band releasing ⇒ the output IS the
    exact pair {BLOCCO_LEVER: BLOCCO_RELEASE} — never a silent {} (a block
    asserted just before disable must be actively cleared; invariant 2)."""
    c = CoolingController()
    s = _hs({"lr": 26.0}, duty=False, fan_pacing=False)
    assert c(s) == {BLOCCO_LEVER: BLOCCO_RELEASE}


def test_call_always_contains_blocco_across_gates():
    """Property: every gate combination yields a BLOCCO opinion (also asserted
    per-cycle across the whole lattice by _run_differential)."""
    for fan_pacing, duty, consenso in itertools.product(
        (False, True), (False, True), ("on", "off", "unavailable")
    ):
        c = CoolingController()
        out = c(_hs({"lr": 26.0}, fan_pacing=fan_pacing, duty=duty,
                    consenso=consenso))
        assert BLOCCO_LEVER in out, (fan_pacing, duty, consenso)
        assert out[BLOCCO_LEVER] in (BLOCCO_BLOCK, BLOCCO_RELEASE)


# --- (e) live-combo conflation pin ---------------------------------------------


def test_regime_off_medium_load_does_not_defeat_duty_cooloff():
    """THE live-combo hazard (STORY §4 risk 7): regime switch OFF + a
    MEDIUM-classifiable load + duty mid-cooloff ⇒ the merged output is BLOCK and
    the setpoint follows band_step (no phase_override effect). A fold that
    conflated `regime_classified` with `regime_driving` would coalesce, emit
    RELEASE (defeating the cooloff) and force-REST the setpoint to 24.75."""
    cfg = _cfg(regime=False)  # regime switch OFF — the live configuration
    c = CoolingController()
    c._duty = DutyState(cooloff_until=T0 + timedelta(minutes=30))
    # MEDIUM-classifiable: converged zones, solar 200 → ratio ≈ 0.19 ≥ 0.10.
    # temp 24.2: band_step (fresh) → RUN → 23.25; a coalescing REST would slam
    # 24.75 (24.2 < enter threshold 24.375) — the two paths are distinguishable.
    s = _hs({"lr": 24.2, "sg": 24.2, "bed": 24.2}, cfg=cfg, solar=200.0)
    out = c(s)
    assert out[BLOCCO_LEVER] == BLOCCO_BLOCK          # duty cooloff survives
    assert out[temperature_lever("climate.lr")] == 23.25  # band_step, no override
    assert c.regime_driving == "low"
    assert c._rs == RegimeState()
    # and the oracle agrees end-to-end
    old = OldPipeline()
    old.duty._duty = DutyState(cooloff_until=T0 + timedelta(minutes=30))
    assert old(s) == out


# --- (f) gate-off is a RESET pass, never a skip ---------------------------------


def test_gate_off_resets_regime_state_and_reenable_starts_fresh():
    cfg_on = _cfg(regime=True)
    hot = {"lr": 25.5, "sg": 25.2, "bed": 25.0}
    c = CoolingController()
    c(_hs(hot, cfg=cfg_on, solar=200.0))
    assert c._rs.house_phase == "run"          # MEDIUM-coalescing established

    # any gate off for ONE pass (duty here) → the pass RESETS, never skips.
    c(_hs(hot, now=T0 + timedelta(minutes=10), cfg=cfg_on, solar=200.0, duty=False))
    assert c._rs == RegimeState()

    # gate back on → the first decision matches a FRESH coordinator's.
    s_back = _hs(hot, now=T0 + timedelta(minutes=20), cfg=cfg_on, solar=200.0)
    fresh = CoolingController()
    assert c.regime_pass(s_back) == fresh.regime_pass(s_back)
    assert c._rs == fresh._rs


# --- (c) harness rows via the REAL build_house_state ----------------------------


# --- (b) end-to-end A/B: old-wired vs new-wired ENGINES ------------------------
# The one oracle that cannot share transliteration bias with the fold: two REAL
# SupervisorEngines over identical scripted hass state — one wired with
# CoolingController, one with the trio composed the old way — must emit EQUAL
# ORDERED service-call streams.


async def _setup_ab_entry(hass, options=None):
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_mock_service,
    )

    from custom_components.villa_hvac.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={}, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    for domain, service in (
        ("climate", "set_preset_mode"), ("climate", "set_temperature"),
        ("fan", "turn_on"), ("fan", "turn_off"), ("fan", "set_percentage"),
        ("switch", "turn_on"), ("switch", "turn_off"),
    ):
        async_mock_service(hass, domain, service)
    return entry


async def _run_wired_engine(hass, entry, controller):
    """One fresh SupervisorEngine wired with `controller`, one actuating pass;
    returns the ordered (domain, service, data) stream it emitted."""
    from homeassistant.const import EVENT_CALL_SERVICE
    from homeassistant.core import callback
    from homeassistant.util import dt as dt_util

    from custom_components.villa_hvac.engine import SupervisorEngine
    from custom_components.villa_hvac.policies import POLICIES

    engine = SupervisorEngine(
        hass, entry, entry.runtime_data, policies=POLICIES,
        controllers=(controller,),
    )
    engine._forecast_ts = dt_util.utcnow()  # skip the live forecast fetch
    stream: list = []

    # @callback so HA dispatches the recorder ON the event loop — a bare lambda
    # is run as an executor job, and adjacent events' appends then race across
    # worker threads, shuffling the captured ORDER nondeterministically (found
    # by the adversarial review: same 3 events, switch/fan swapped ~1-in-3 runs).
    @callback
    def _record(e):
        stream.append((
            e.data.get("domain"), e.data.get("service"),
            tuple(sorted((e.data.get("service_data") or {}).items())),
        ))

    unsub = hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    try:
        await engine._run()
        await hass.async_block_till_done()
    finally:
        unsub()
    return stream


async def _ab_streams(hass, entry, *, seed_duty=None):
    new_ctrl = CoolingController()
    old_ctrl = OldPipeline()
    if seed_duty is not None:
        new_ctrl._duty = seed_duty
        old_ctrl.duty._duty = seed_duty
    stream_new = await _run_wired_engine(hass, entry, new_ctrl)
    stream_old = await _run_wired_engine(hass, entry, old_ctrl)
    return stream_new, stream_old


async def test_ab_live_combo_band_run(hass):
    """A/B: the live combo (fan_pacing+duty ON) driving a warm Salotto — band
    setpoint/fan/manuale + duty BLOCCO, identical ordered streams."""
    hass.states.async_set(
        "climate.salotto_termostato_2", "cool",
        {"preset_mode": "comfort", "temperature": 24.0},
    )
    hass.states.async_set("sensor.clima_salotto", "26.0")
    hass.states.async_set("binary_sensor.fancoil_salotto_valvola", "on")
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")
    hass.states.async_set("fan.fancoil_salotto", "on", {"percentage": 0})
    hass.states.async_set("switch.fancoil_salotto_manuale", "off")
    entry = await _setup_ab_entry(hass)
    hass.states.async_set("switch.fan_pacing", "on")
    hass.states.async_set("switch.duty_cycle", "on")

    stream_new, stream_old = await _ab_streams(hass, entry)

    assert stream_new == stream_old
    assert any(s == "set_temperature" for _, s, _ in stream_new)  # band actually wrote


async def test_ab_duty_blocks_after_stint(hass):
    """A/B: stint exceeded ⇒ both wirings assert BLOCCO in the same stream slot."""
    from datetime import timedelta as td

    from homeassistant.util import dt as dt_util

    from custom_components.villa_hvac.const import (
        CONSENSO_BLOCCO,
        OPT_DUTY_COOLOFF as OC,
        OPT_DUTY_MAX_STINT as OS,
    )

    hass.states.async_set(
        "climate.salotto_termostato_2", "cool", {"preset_mode": "comfort"}
    )
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")
    hass.states.async_set(CONSENSO_BLOCCO, "off")
    entry = await _setup_ab_entry(hass, options={OS: 15, OC: 30})
    hass.states.async_set("switch.duty_cycle", "on")

    seed = DutyState(stint_start=dt_util.utcnow() - td(minutes=20))
    stream_new, stream_old = await _ab_streams(hass, entry, seed_duty=seed)

    assert stream_new == stream_old
    assert any(
        s == "turn_on" and dict(d).get("entity_id") == CONSENSO_BLOCCO
        for _, s, d in stream_new
    )


async def test_ab_duty_off_releases_blocked_villa(hass):
    """A/B: duty OFF + a live block ⇒ both wirings actively clear it."""
    from custom_components.villa_hvac.const import CONSENSO_BLOCCO

    hass.states.async_set(
        "climate.salotto_termostato_2", "cool", {"preset_mode": "comfort"}
    )
    hass.states.async_set(CONSENSO_BLOCCO, "on")
    entry = await _setup_ab_entry(hass)  # duty defaults OFF

    stream_new, stream_old = await _ab_streams(hass, entry)

    assert stream_new == stream_old
    assert any(
        s == "turn_off" and dict(d).get("entity_id") == CONSENSO_BLOCCO
        for _, s, d in stream_new
    )


async def test_ab_free_cool_bp_and_band_yields(hass):
    """A/B: free-cool forces BP and the band yields — no setpoint pushed onto
    the BP zone in either wiring."""
    hass.states.async_set(
        "climate.salotto_termostato_2", "cool",
        {"preset_mode": "comfort", "temperature": 24.0},
    )
    hass.states.async_set("sensor.clima_salotto", "26.0")
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "20.0")  # < 22
    entry = await _setup_ab_entry(hass)
    hass.states.async_set("switch.fan_pacing", "on")

    stream_new, stream_old = await _ab_streams(hass, entry)

    assert stream_new == stream_old
    assert any(
        s == "set_preset_mode"
        and dict(d).get("preset_mode") == "building_protection"
        for _, s, d in stream_new
    )
    assert not any(s == "set_temperature" for _, s, _ in stream_new)


async def test_differential_on_real_build_house_state(hass):
    """A subset of rows built by the real build_house_state (catches a gate-source
    swap: duty_enabled/fan_pacing_enabled must come from the same switches the
    old engine read)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.villa_hvac.const import DOMAIN
    from custom_components.villa_hvac.engine import build_house_state

    hass.states.async_set(
        "climate.salotto_termostato_2", "cool",
        {"preset_mode": "comfort", "temperature": 24.0},
    )
    hass.states.async_set("sensor.clima_salotto", "26.0")
    hass.states.async_set("binary_sensor.fancoil_salotto_valvola", "on")
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.fan_pacing", "on")
    hass.states.async_set("switch.duty_cycle", "on")

    old, new = OldPipeline(), CoolingController()
    # gate switches VARIED per cycle (asymmetric: on/on, on/off, off/on) so a
    # duty<->fan_pacing gate-SOURCE cross-swap in build_house_state cannot pass
    # (review finding: identical all-on cycles were blind to it).
    gate_script = (("on", "on"), ("on", "off"), ("off", "on"), ("on", "on"))
    for duty_sw, fan_sw in gate_script:
        hass.states.async_set("switch.duty_cycle", duty_sw)
        hass.states.async_set("switch.fan_pacing", fan_sw)
        raw = build_house_state(hass, entry, entry.runtime_data)
        state = annotate_centers(raw, max_age=SCHEDULE_MAX_AGE)
        assert state.duty_enabled is (duty_sw == "on")
        assert state.fan_pacing_enabled is (fan_sw == "on")
        out_old, out_new = old(state), new(state)
        assert out_new == out_old, (duty_sw, fan_sw)
        assert list(out_new.keys()) == list(out_old.keys()), (duty_sw, fan_sw)
        assert _post_new(new) == _post_old(old), (duty_sw, fan_sw)
    # sanity: the live combo actually produced band writes, not just BLOCCO.
    assert temperature_lever("climate.salotto_termostato_2") in out_new
