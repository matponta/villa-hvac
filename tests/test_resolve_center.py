"""R1 (Tier-1): resolve_center / annotate_centers — the ONE per-leader band center.

P1 test gate (STORY_TIER1_COOLING_CONTROLLER §2):
  (a) golden matrix: resolve_center == the direct planner_ref + compose_center
      wiring it replaces (precedence + single clamp site), across
      {pv} × {precool} × {planner-eligible} × {schedule} × {mode};
  (b) engine-level end-to-end pre-cool pin (the function matrix cannot see the
      _cycle wiring order);
  (c) engine ordering pins: the resolution reflects pv_mode / the schedule, so
      annotate_centers provably runs AFTER _pv_bias_apply / _maybe_refresh_schedule;
  (d) loud fallback: an eligible leader with no resolved center on an actuating
      pass WARNs once (never a silent degrade to the base center).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import itertools

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.villa_hvac.const import DOMAIN, SCHEDULE_MAX_AGE
from custom_components.villa_hvac.supervisor import (
    CenterPoint,
    CenterSchedule,
    HouseState,
    ZoneCenterSchedule,
    ZoneSnapshot,
    annotate_centers,
    compose_center,
    planner_ref,
    resolve_center,
)

T0 = datetime(2026, 7, 3, 12, 0, 0)
CLIMATE = "climate.salotto_termostato_2"

CEIL = 27.0
FLOOR = 22.0


def _leader(zid="a", *, temp=25.0, eligible=False, **kw):
    base = dict(
        zone_id=zid, name=zid, climate=f"climate.{zid}", emitter="fancoil",
        temp=temp, model_planner_eligible=eligible,
        fancoil_units=((f"fan.{zid}", f"switch.{zid}_man"),),
    )
    base.update(kw)
    return ZoneSnapshot(**base)


def _sched(zid, center, *, created):
    return CenterSchedule(
        zones={zid: ZoneCenterSchedule(
            zone_id=zid, points=(CenterPoint(minute=0, center=center),), eligible=True,
        )},
        created_at=created,
    )


def _house(zones, **kw):
    args = dict(
        now=T0, zones={z.zone_id: z for z in zones},
        season="summer", house_mode="Casa", house_setpoint=24.0, mode_offset=0.0,
        duty_enabled=True, precool=False, precool_offset=1.5,
        duty_comfort_max=CEIL, comfort_floor=FLOOR,
        fan_pacing_enabled=True,
        unified_planner_enabled=True,
    )
    args.update(kw)
    return HouseState(**args)


def _old_wiring(zone, state):
    """The v0.38.0 wiring resolve_center replaces — a byte-faithful port of the
    inline block at policies.py:489-511 / engine._center_compositions:911-953:
    planner_ref (its own clamp) else the compose_center ladder. The reference
    implementation for the golden matrix."""
    base = state.house_setpoint + state.mode_offset
    comp = compose_center(
        base=base,
        pv_mode=state.pv_mode, pv_floor=state.pv_floor,
        pv_coast_relax=state.pv_coast_relax,
        precool=state.precool, precool_offset=state.precool_offset,
        duty_enabled=state.duty_enabled,
        comfort_ceiling=state.duty_comfort_max,
        comfort_floor=state.comfort_floor,
    )
    ref = planner_ref(
        state.center_schedule, zone_id=zone.zone_id, now=state.now,
        planner_eligible=zone.model_planner_eligible,
        unified_enabled=state.unified_planner_enabled,
        center_base=base, comfort_floor=state.comfort_floor,
        comfort_ceiling=state.duty_comfort_max, max_age=SCHEDULE_MAX_AGE,
    )
    center = ref if ref is not None else comp.center
    source = "planner" if ref is not None else comp.source
    return center, base, source, comp.floored, ref is not None


# --- (a) golden matrix: resolve_center == the wiring it replaces ---------------

_PV = (None, "bank", "coast", "hold")
_PRECOOL = (False, True)
_ELIGIBLE = (False, True)
_SCHEDULE = ("fresh", "stale", "none")
_MODE = ("comfort", "setback")


@pytest.mark.parametrize(
    "pv, precool, eligible, schedule, mode",
    list(itertools.product(_PV, _PRECOOL, _ELIGIBLE, _SCHEDULE, _MODE)),
)
def test_resolve_center_matches_old_wiring(pv, precool, eligible, schedule, mode):
    zone = _leader(eligible=eligible)
    sched = {
        "fresh": _sched("a", 23.0, created=T0),
        "stale": _sched("a", 23.0, created=T0 - timedelta(hours=3)),  # > 90 min
        "none": None,
    }[schedule]
    state = _house(
        [zone],
        pv_mode=pv,
        pv_floor=21.5 if pv == "bank" else None,   # below FLOOR -> exercises flooring
        pv_coast_relax=1.5,
        precool=precool,
        mode_offset=0.0 if mode == "comfort" else 5.0,  # base 24 vs 29 (> ceiling)
        center_schedule=sched,
    )

    res = resolve_center(zone, state, max_age=SCHEDULE_MAX_AGE)
    center, base, source, floored, driven = _old_wiring(zone, state)

    assert res is not None
    assert res.center == center
    assert res.base == base
    assert res.source == source
    assert res.floored == floored
    assert res.planner_driven == driven
    # single clamp site: the resolved center never escapes the comfort bounds
    # unless the BASE itself sits outside them (a deep setback is the mode's
    # choice, not a feature).
    if base <= CEIL:
        assert res.center <= max(CEIL, base)
    assert res.center >= min(FLOOR, base)


def test_resolve_center_none_without_base():
    zone = _leader()
    state = _house([zone], house_setpoint=None)
    assert resolve_center(zone, state, max_age=SCHEDULE_MAX_AGE) is None


# --- annotate_centers: eligibility mirror of the FanBandController -------------

def test_annotate_sets_fields_on_eligible_leader():
    state = annotate_centers(
        _house([_leader()], precool=True), max_age=SCHEDULE_MAX_AGE
    )
    z = state.zones["a"]
    assert z.resolved_center == 22.5          # 24 - 1.5 precool
    assert z.center_source == "precool"
    assert z.center_floored is False
    assert z.planner_driven is False


def test_annotate_skips_ineligible_zones():
    zones = [
        _leader("dis", enabled=False),
        _leader("pause", paused=True),
        _leader("bed", bedroom=True),
        replace(_leader("follower"), follows="a", climate=None),
        ZoneSnapshot(zone_id="rad", name="rad", climate="climate.rad",
                     emitter="radiant", temp=25.0),
        _leader("ok"),
    ]
    state = annotate_centers(
        _house(zones, night_active=True), max_age=SCHEDULE_MAX_AGE
    )
    assert state.zones["ok"].resolved_center is not None
    for zid in ("dis", "pause", "bed", "follower", "rad"):
        z = state.zones[zid]
        assert z.resolved_center is None
        assert z.center_source == "none"
        assert z.center_floored is False and z.planner_driven is False


def test_annotate_skips_all_when_free_cooling():
    state = annotate_centers(
        _house(
            [_leader()], free_cool_enabled=True, free_cool_threshold=22.0,
            outdoor_temp=20.0,
        ),
        max_age=SCHEDULE_MAX_AGE,
    )
    assert state.zones["a"].resolved_center is None


def test_annotate_identity_without_base():
    state = _house([_leader()], house_setpoint=None)
    assert annotate_centers(state, max_age=SCHEDULE_MAX_AGE) is state


# --- engine-level pins (b)/(c)/(d): the _cycle wiring the matrix cannot see ----

async def _setup_band_engine(hass, *, salotto_temp="24.5"):
    """Real integration setup with the live combo (fan_pacing + duty ON) and a
    warm Salotto under band control."""
    hass.states.async_set(
        CLIMATE, "cool", {"preset_mode": "comfort", "temperature": 24.0}
    )  # summer; house_setpoint default 24 + Casa offset 0 -> base center 24
    hass.states.async_set("sensor.clima_salotto", salotto_temp)
    hass.states.async_set("binary_sensor.fancoil_salotto_valvola", "on")
    hass.states.async_set("binary_sensor.ct_consenso_freddo_villa", "on")
    hass.states.async_set("fan.fancoil_salotto", "on", {"percentage": 0})
    hass.states.async_set("switch.fancoil_salotto_manuale", "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.supervisor", "on")
    hass.states.async_set("switch.steady_pacing", "on")
    hass.states.async_set("switch.paced_living_room", "on")
    hass.states.async_set("switch.duty_cycle", "on")
    return entry


async def test_precool_drives_band_setpoint_end_to_end(hass):
    """(b) The pin the function matrix cannot see: with a hot forecast ahead the
    BAND setpoint lands at (base - precool_offset) - slam = 24 - 1.5 - 0.75 =
    21.75 — proving the annotate call sits in the right _cycle slot and the band
    actually slams the precool-shifted center (a silent fallback to the base
    center would write 23.25 and fail here)."""
    from homeassistant.util import dt as dt_util

    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    hass.states.async_set("sensor.gw3000a_outdoor_temperature", "25.0")  # cool now
    engine._forecast = [(dt_util.utcnow() + timedelta(hours=1), 33.0)]  # hot peak
    engine._forecast_ts = dt_util.utcnow()
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    salotto = [c for c in temps if c.data["entity_id"] == CLIMATE]
    assert salotto and salotto[-1].data["temperature"] == 22.5
    comps = engine.plan_view.center_compositions
    assert comps["living_room"]["source"] == "precool"
    assert comps["living_room"]["center"] == 22.5


async def test_annotate_runs_after_pv_bias(hass):
    """(c) Ordering pin: a pv_mode attached by _pv_bias_apply is visible to the
    resolution — annotate_centers runs AFTER it (wrong order -> base center 24
    -> 23.25 write -> fail)."""
    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    engine._pv_bias_apply = lambda state: replace(
        state, pv_mode="bank", pv_floor=22.0
    )
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    comps = engine.plan_view.center_compositions
    assert comps["living_room"]["center"] == 22.0
    assert comps["living_room"]["source"] == "pv_bank"


async def test_annotate_runs_after_schedule_attach(hass):
    """(c) Ordering pin: a fresh planner schedule attached by
    _maybe_refresh_schedule drives the resolution for an eligible room —
    annotate_centers runs AFTER the attach."""
    from homeassistant.util import dt as dt_util

    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    # D1: make living_room planner-eligible (abc identified + k converged).
    engine.thermal.load({
        "living_room": {"a": 0.03, "b": 0.0008, "c": 0.0, "k": 1.2, "p": [0.0] * 9,
                        "p_k": 0.0, "n": 100, "n_k": 100, "s_hi": 400.0},
    })
    sched = _sched("living_room", 23.0, created=dt_util.utcnow())
    engine._maybe_refresh_schedule = lambda state: replace(
        state, center_schedule=sched, unified_planner_enabled=True
    )
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    comps = engine.plan_view.center_compositions
    assert comps["living_room"]["center"] == 23.0
    assert comps["living_room"]["planner_driven"] is True
    assert comps["living_room"]["source"] == "planner"


async def test_annotate_runs_after_away_return_override(hass):
    """(c) Ordering pin: the #8 effective-mode override (which rewrites
    mode_offset and therefore the BASE center) is visible to the resolution —
    annotate_centers runs AFTER away_return.apply. With a +5 setback override the
    band must REST at 29.75 (29 + 0.75); annotating BEFORE the override would
    resolve the raw base 24 -> RUN at 23.25 -> fail."""
    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    engine.away_return.apply = lambda state, hass_, entry_, commit: replace(
        state, mode_offset=5.0
    )
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_preset_mode")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    salotto = [c for c in temps if c.data["entity_id"] == CLIMATE]
    assert salotto and salotto[-1].data["temperature"] == 29.0


async def test_cycle_invokes_unresolved_center_check(hass, caplog, monkeypatch):
    """(d) The loud-fallback WIRING pin: with annotate_centers broken (no-op), a
    real actuating _cycle must WARN — proving _check_unresolved_centers is
    actually called from the actuating branch (deleting that call would
    otherwise leave every test green while the mitigation silently vanished)."""
    import custom_components.villa_hvac.engine as engine_mod

    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    monkeypatch.setattr(
        engine_mod, "annotate_centers", lambda state, *, max_age: state
    )
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    assert "living_room" in engine._unresolved_center
    assert "no resolved band center" in caplog.text


async def test_actuating_pass_resolves_every_eligible_leader(hass, caplog):
    """(d) Through the real engine path, no eligible cooling leader ever reaches
    an actuating pass unresolved (the annotate call is in place)."""
    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "fan", "turn_on")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await engine.request_run()

    assert engine._unresolved_center == set()
    assert "no resolved band center" not in caplog.text


async def test_unresolved_center_warns_once(hass, caplog):
    """(d) The loud fallback itself: an UNANNOTATED state reaching the check WARNs
    once per zone (not every cycle), and a resolved cycle clears it — never a
    silent degrade to the base center."""
    entry = await _setup_band_engine(hass)
    engine = entry.runtime_data.engine
    from custom_components.villa_hvac.engine import build_house_state

    raw = build_house_state(hass, entry, entry.runtime_data)  # no annotation
    assert raw.zones["living_room"].resolved_center is None

    engine._check_unresolved_centers(raw)
    assert "living_room" in engine._unresolved_center
    first = caplog.text.count("no resolved band center")
    assert first >= 1
    engine._check_unresolved_centers(raw)  # same condition -> no re-warn
    assert caplog.text.count("no resolved band center") == first

    fixed = annotate_centers(raw, max_age=SCHEDULE_MAX_AGE)
    engine._check_unresolved_centers(fixed)
    assert "living_room" not in engine._unresolved_center
