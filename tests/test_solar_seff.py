"""S_eff per-facade solar (STORY_SEFF) — pure-law geometry pins + the inert vN
plumbing (engine feed + structural flag darkness).

The law pins follow STORY_SEFF §7.5; the structural-darkness pins are the
adversarial-review requirement that the option can never light S_eff up while
any §6 b-consumer still reads house GHI.
"""
from __future__ import annotations

import math

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.villa_hvac import supervisor_config
from custom_components.villa_hvac.const import (
    DOMAIN,
    OPT_SEFF_ENABLED,
    SHADING_AZIMUTH_BANDS,
    SOLAR_RADIATION,
)
from custom_components.villa_hvac.engine import build_house_state
from custom_components.villa_hvac.supervisor import (
    Aperture,
    CoverInfo,
    SEFF_COVER_FLOOR,
    SEFF_DIFFUSE_FRACTION,
    SEFF_DIFFUSE_VERTICAL,
    SEFF_FACADE_NORMALS,
    SEFF_RB_MAX,
    SEFF_SOURCE_DEGRADED,
    SEFF_SOURCE_FACADE,
    SEFF_SOURCE_FALLBACK,
    SEFF_SOURCE_GHI,
    SEFF_UNITS_GHI,
    cover_transmission,
    facade_beam_factor,
    units_tag,
    zone_apertures,
    zone_effective_solar,
    zone_solar_curves,
)
from custom_components.villa_hvac.supervisor_config import SupervisorConfig

GHI = 800.0
SOUTH = SEFF_FACADE_NORMALS["south"]  # 225 — verified real SW facade
WEST = SEFF_FACADE_NORMALS["west"]    # 292 — verified real WNW facade
OPEN_W = Aperture(normal_deg=WEST, transmission=1.0)
OPEN_S = Aperture(normal_deg=SOUTH, transmission=1.0)


# --- facade_beam_factor -------------------------------------------------------


def test_beam_zero_behind_facade():
    # sun az 100 vs SW facade (225): cos delta < 0 -> no beam
    assert facade_beam_factor(40.0, 100.0, SOUTH) == 0.0


def test_beam_zero_at_low_elevation():
    # at/below 3 deg the 1/sin term is ill-conditioned -> hard zero
    assert facade_beam_factor(3.0, WEST, WEST) == 0.0
    assert facade_beam_factor(1.0, WEST, WEST) == 0.0


def test_beam_clamped_at_grazing_sun():
    # el 8 square-on: cos8/sin8 ~ 7.1 -> clamped to SEFF_RB_MAX
    assert facade_beam_factor(8.0, WEST, WEST) == SEFF_RB_MAX


def test_beam_square_on_moderate_sun():
    # el 34, az 270 vs WNW (292): rb = cos34*cos22/sin34 ~ 1.375, unclamped
    rb = facade_beam_factor(34.0, 270.0, WEST)
    expected = math.cos(math.radians(34)) * math.cos(math.radians(22)) / math.sin(
        math.radians(34)
    )
    assert rb == pytest.approx(expected)
    assert rb < SEFF_RB_MAX


# --- cover_transmission -------------------------------------------------------


def test_transmission_closed_floor():
    assert cover_transmission(0) == pytest.approx(SEFF_COVER_FLOOR)


def test_transmission_open_and_half():
    assert cover_transmission(100) == pytest.approx(1.0)
    assert cover_transmission(50) == pytest.approx(0.6)


def test_transmission_none_assumes_open():
    # comfort-safe direction: over-state gain -> fan sized up
    assert cover_transmission(None) == 1.0


def test_transmission_clamps_out_of_range():
    assert cover_transmission(-20) == pytest.approx(SEFF_COVER_FLOOR)
    assert cover_transmission(140) == pytest.approx(1.0)


# --- zone_effective_solar -----------------------------------------------------


def test_no_apertures_is_ghi_identity():
    assert zone_effective_solar(GHI, 40.0, 200.0, ()) == (GHI, SEFF_SOURCE_GHI)


def test_ghi_none_is_fallback():
    assert zone_effective_solar(None, 40.0, 200.0, (OPEN_W,)) == (
        None, SEFF_SOURCE_FALLBACK,
    )


def test_sun_missing_with_apertures_is_ghi_valued_fallback():
    # control uses the GHI value (over-states gain, safe); estimator will skip
    assert zone_effective_solar(GHI, None, 200.0, (OPEN_W,)) == (
        GHI, SEFF_SOURCE_FALLBACK,
    )
    assert zone_effective_solar(GHI, 40.0, None, (OPEN_W,)) == (
        GHI, SEFF_SOURCE_FALLBACK,
    )


def test_night_is_exact_zero():
    # keeps night windows clean for {a,c}
    assert zone_effective_solar(GHI, -5.0, 300.0, (OPEN_W,)) == (
        0.0, SEFF_SOURCE_FACADE,
    )


def test_beam_left_facade_keeps_diffuse_floor():
    # noon (az 180, el 68) on the WNW facade: beam behind -> diffuse only,
    # never zero while the sky is bright (b stays excitable)
    val, src = zone_effective_solar(GHI, 68.0, 180.0, (OPEN_W,))
    assert src == SEFF_SOURCE_FACADE
    assert val == pytest.approx(GHI * SEFF_DIFFUSE_VERTICAL)


def test_closed_cover_scales_by_floor():
    closed = Aperture(normal_deg=WEST, transmission=SEFF_COVER_FLOOR)
    val_open, _ = zone_effective_solar(GHI, 34.0, 270.0, (OPEN_W,))
    val_closed, _ = zone_effective_solar(GHI, 34.0, 270.0, (closed,))
    assert val_closed == pytest.approx(val_open * SEFF_COVER_FLOOR)


def test_degraded_aperture_marks_source():
    degraded = Aperture(normal_deg=WEST, transmission=1.0, degraded=True)
    _, src = zone_effective_solar(GHI, 34.0, 270.0, (degraded,))
    assert src == SEFF_SOURCE_DEGRADED


def test_wnw_phase_fix():
    # the whole point: on the WNW facade, S_eff relative to GHI at 17:30
    # (az 270 / el 34) must exceed the 13:20 value (az 180 / el 68)
    evening, _ = zone_effective_solar(GHI, 34.0, 270.0, (OPEN_W,))
    noon, _ = zone_effective_solar(GHI, 68.0, 180.0, (OPEN_W,))
    assert evening > noon
    assert evening > GHI  # a lit WNW pane out-gains the horizontal sensor


def test_two_facade_beam_sum_is_zone_clamped():
    # az 258 / el 10 sits between the two normals: BOTH per-facade factors hit
    # the clamp; without the zone-level beam-sum clamp this would be ~4.9x GHI
    val, _ = zone_effective_solar(GHI, 10.0, 258.0, (OPEN_S, OPEN_W))
    bound = GHI * (
        (1.0 - SEFF_DIFFUSE_FRACTION) * SEFF_RB_MAX + 2 * SEFF_DIFFUSE_VERTICAL
    )
    assert val == pytest.approx(bound)  # = 2.69x GHI, not 4.9x


def test_two_facade_full_scan_bounded():
    # STORY_SEFF §1.6: the main_bedroom two-facade sum never exceeds
    # 0.75*RB_MAX + 2*0.22 = 2.69x GHI anywhere in the sky
    bound = (1.0 - SEFF_DIFFUSE_FRACTION) * SEFF_RB_MAX + 2 * SEFF_DIFFUSE_VERTICAL
    for el in range(1, 87, 3):
        for az in range(0, 360, 5):
            val, _ = zone_effective_solar(GHI, float(el), float(az), (OPEN_S, OPEN_W))
            assert val <= GHI * bound + 1e-9


def test_normals_cross_pin_shading_bands():
    # the two encodings of the villa rotation must agree: each verified normal
    # sits inside its own label's shading azimuth band
    s_lo, s_hi = SHADING_AZIMUTH_BANDS["south"]
    w_lo, w_hi = SHADING_AZIMUTH_BANDS["west"]
    assert s_lo <= SOUTH <= s_hi
    assert w_lo <= WEST <= w_hi


# --- zone_apertures -----------------------------------------------------------


def _cover(entity, orientation, zone, pos):
    return CoverInfo(
        entity_id=entity, orientation=orientation, zone=zone, current_position=pos
    )


def test_apertures_group_same_facade_mean_g():
    aps = zone_apertures(
        [_cover("cover.a", "west", "office", 100), _cover("cover.b", "west", "office", 0)]
    )["office"]
    assert len(aps) == 1
    assert aps[0].cover_count == 2
    assert aps[0].transmission == pytest.approx((1.0 + SEFF_COVER_FLOOR) / 2)


def test_apertures_two_facades_and_degraded():
    aps = zone_apertures(
        [
            _cover("cover.g", "west", "main_bedroom", None),
            _cover("cover.p", "south", "main_bedroom", 50),
        ]
    )["main_bedroom"]
    assert [ap.normal_deg for ap in aps] == [SOUTH, WEST]
    west = aps[1]
    assert west.transmission == 1.0 and west.degraded  # None -> assume open + flag


def test_apertures_skip_unmapped_label_and_no_zone():
    aps = zone_apertures(
        [
            _cover("cover.n", "north", "office", 50),  # unverified normal -> excluded
            _cover("cover.z", "west", None, 50),       # no zone -> excluded
        ]
    )
    assert aps == {}


def test_blocked_cover_still_contributes():
    # blocked = "don't actuate", not "no glass"
    blocked = CoverInfo(
        entity_id="cover.b", orientation="south", zone="studio_v",
        blocked=True, current_position=0,
    )
    aps = zone_apertures([blocked])["studio_v"]
    assert aps[0].transmission == pytest.approx(SEFF_COVER_FLOOR)


# --- units_tag ----------------------------------------------------------------


def test_units_tag_verified_rooms():
    # review pin: the office is on the WEST (292) facade, not south
    main = zone_apertures(
        [
            _cover("cover.grande", "west", "main_bedroom", 100),
            _cover("cover.piccola", "south", "main_bedroom", 100),
        ]
    )["main_bedroom"]
    office = zone_apertures([_cover("cover.ps", "west", "office", 100)])["office"]
    studio = zone_apertures([_cover("cover.sv", "south", "studio_v", 100)])["studio_v"]
    assert units_tag(main) == "seff1:225x1,292x1"
    assert units_tag(office) == "seff1:292x1"
    assert units_tag(studio) == "seff1:225x1"
    assert units_tag(()) == SEFF_UNITS_GHI


def test_units_tag_encodes_cover_count():
    # a second cover on an already-fitted facade changes the mean-g scale ->
    # the tag must flip (drives the b re-wipe)
    one = zone_apertures([_cover("cover.a", "west", "office", 100)])["office"]
    two = zone_apertures(
        [_cover("cover.a", "west", "office", 100), _cover("cover.b", "west", "office", 100)]
    )["office"]
    assert units_tag(one) == "seff1:292x1"
    assert units_tag(two) == "seff1:292x2"
    assert units_tag(one) != units_tag(two)


# --- zone_solar_curves --------------------------------------------------------


def test_curves_match_law_elementwise():
    ghi_curve = [900.0, 600.0, 300.0]
    els = [60.0, 34.0, 10.0]
    azs = [180.0, 270.0, 292.0]
    curves = zone_solar_curves(ghi_curve, els, azs, {"office": (OPEN_W,)})
    for i in range(3):
        expected, _ = zone_effective_solar(ghi_curve[i], els[i], azs[i], (OPEN_W,))
        assert curves["office"][i] == pytest.approx(expected)


def test_curves_flat_mode_uses_live_ratio():
    # flat solar model must not silently sim on the house curve while actuation
    # runs on S_eff: propagate the live per-zone ratio
    ghi_curve = [500.0, 400.0]
    curves = zone_solar_curves(
        ghi_curve, [], [], {"office": (OPEN_W,)}, live_ratio={"office": 1.25}
    )
    assert curves["office"] == [625.0, 500.0]


def test_curves_flat_mode_without_ratio_is_empty():
    assert zone_solar_curves([500.0], [], [], {"office": (OPEN_W,)}) == {}
    assert zone_solar_curves([], [60.0], [180.0], {"office": (OPEN_W,)}) == {}


# --- structural flag darkness (SupervisorConfig) -------------------------------


def test_seff_option_is_structurally_dark():
    # adversarial-review pin: until SEFF_CONSUMERS_READY flips in the release
    # that completes the consumer table, the option can NEVER enable S_eff
    assert supervisor_config.SEFF_CONSUMERS_READY is False
    cfg = SupervisorConfig.from_options({OPT_SEFF_ENABLED: True})
    assert cfg.seff_enabled is False


def test_seff_option_flows_once_consumers_ready(monkeypatch):
    monkeypatch.setattr(supervisor_config, "SEFF_CONSUMERS_READY", True)
    assert SupervisorConfig.from_options({OPT_SEFF_ENABLED: True}).seff_enabled is True
    assert SupervisorConfig.from_options({}).seff_enabled is False  # still opt-in


# --- engine feed (inert vN plumbing) -------------------------------------------


PADRONALE_COVERS = (
    CoverInfo(entity_id="cover.grande_camera", orientation="west", zone="main_bedroom"),
    CoverInfo(entity_id="cover.piccola_camera", orientation="south", zone="main_bedroom"),
)


async def _setup(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={}, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_snapshot_is_ghi_identity_and_diagnostics_carry_facade(hass):
    """Flag off (vN always): every snapshot carries the GHI identity — while
    engine.last_s_eff exposes the would-be facade values for live validation."""
    entry = await _setup(hass, options={OPT_SEFF_ENABLED: True})  # dark anyway
    hass.states.async_set(SOLAR_RADIATION, str(GHI))
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 270.0, "elevation": 34.0}
    )
    hass.states.async_set("cover.grande_camera", "open", {"current_position": 100})
    hass.states.async_set("cover.piccola_camera", "open", {"current_position": 100})

    state = build_house_state(
        hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS
    )
    z = state.zones["main_bedroom"]
    assert z.s_eff == GHI
    assert z.s_eff_source == SEFF_SOURCE_GHI
    assert z.s_eff_units == SEFF_UNITS_GHI

    val, src, units = entry.runtime_data.engine.last_s_eff["main_bedroom"]
    expected, _ = zone_effective_solar(GHI, 34.0, 270.0, (OPEN_S, OPEN_W))
    assert val == pytest.approx(expected)
    assert val != GHI
    assert src == SEFF_SOURCE_FACADE
    assert units == "seff1:225x1,292x1"


async def test_snapshot_carries_facade_values_when_lit(hass, monkeypatch):
    """The ONE engine switch site: with the flag structurally on, the snapshot
    carries the facade S_eff (consumers switched to z.s_eff would diverge from
    GHI — the wiring-pin direction for vN+1)."""
    monkeypatch.setattr(supervisor_config, "SEFF_CONSUMERS_READY", True)
    entry = await _setup(hass, options={OPT_SEFF_ENABLED: True})
    hass.states.async_set(SOLAR_RADIATION, str(GHI))
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 270.0, "elevation": 34.0}
    )
    hass.states.async_set("cover.grande_camera", "open", {"current_position": 100})
    hass.states.async_set("cover.piccola_camera", "open", {"current_position": 100})

    state = build_house_state(
        hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS
    )
    z = state.zones["main_bedroom"]
    expected, _ = zone_effective_solar(GHI, 34.0, 270.0, (OPEN_S, OPEN_W))
    assert z.s_eff == pytest.approx(expected)
    assert z.s_eff != state.solar
    assert z.s_eff_source == SEFF_SOURCE_FACADE
    assert z.s_eff_units == "seff1:225x1,292x1"
    # a leader with no labeled cover stays on the GHI identity
    salotto = state.zones["living_room"]
    assert salotto.s_eff == GHI
    assert salotto.s_eff_units == SEFF_UNITS_GHI


async def test_garbage_sun_attribute_degrades_to_fallback(hass, monkeypatch):
    """A non-numeric sun.sun attribute must degrade the zone to the "fallback"
    source (GHI value), never crash the cycle — review pin (the S_eff feed runs
    in states the old shading-only consumer never touched, e.g. winter)."""
    monkeypatch.setattr(supervisor_config, "SEFF_CONSUMERS_READY", True)
    entry = await _setup(hass, options={OPT_SEFF_ENABLED: True})
    hass.states.async_set(SOLAR_RADIATION, str(GHI))
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": "garbage", "elevation": 34.0}
    )
    hass.states.async_set("cover.grande_camera", "open", {"current_position": 100})

    state = build_house_state(
        hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS[:1]
    )
    z = state.zones["main_bedroom"]
    assert z.s_eff == GHI
    assert z.s_eff_source == SEFF_SOURCE_FALLBACK


async def test_ensure_units_seam_fires_with_learning_disabled(hass, monkeypatch):
    """CRITICAL review pin (STORY_SEFF §4.2): the units rebase runs at the
    consumption seam — even with OPT_MODEL_ENABLED off (the observe path never
    ticks) and with no temperature/valve data, a stored GHI-fitted b must be
    rebased before model_for feeds control in S_eff units."""
    from custom_components.villa_hvac.const import (
        COOL_GAIN_SOLAR, MODEL_P0_PASSIVE, OPT_MODEL_ENABLED,  # noqa: F811
    )

    monkeypatch.setattr(supervisor_config, "SEFF_CONSUMERS_READY", True)
    entry = await _setup(
        hass, options={OPT_SEFF_ENABLED: True, OPT_MODEL_ENABLED: False}
    )
    thermal = entry.runtime_data.engine.thermal
    thermal.load({
        "main_bedroom": {
            "a": 0.02, "b": 0.004, "c": 0.1, "k": 0.9,
            "p": [0.1, 0.01, 0.01, 0.01, 5e-6, 0.01, 0.01, 0.01, 2.0],
            "p_k": 1.0, "n": 200, "n_k": 30, "s_hi": 900.0,
        }
    })
    hass.states.async_set(SOLAR_RADIATION, str(GHI))
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 270.0, "elevation": 34.0}
    )
    hass.states.async_set("cover.grande_camera", "open", {"current_position": 100})
    hass.states.async_set("cover.piccola_camera", "open", {"current_position": 100})

    build_house_state(hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS)

    p = thermal.params["main_bedroom"]
    assert thermal._s_units["main_bedroom"] == "seff1:225x1,292x1"
    assert p.b == COOL_GAIN_SOLAR          # wiped to prior in the new units
    assert p.s_hi == 0.0                   # excitation re-gates in S_eff units
    assert p.p[4] == MODEL_P0_PASSIVE[1]   # b variance reopened
    assert p.p[1] == p.p[3] == p.p[5] == p.p[7] == 0.0  # b cross terms cleared
    assert p.a == 0.02 and p.c == 0.1 and p.k == 0.9    # a, c, k kept
    assert p.n == 200 and p.n_k == 30      # counts kept (blend stays confident)
    assert p.p[0] == 0.1 and p.p[8] == 2.0  # a, c variances kept

    # Idempotent: a second build with the same tag must not rebase again.
    build_house_state(hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS)
    assert thermal.params["main_bedroom"] == p


async def test_unknown_cover_position_degrades_source(hass, monkeypatch):
    monkeypatch.setattr(supervisor_config, "SEFF_CONSUMERS_READY", True)
    entry = await _setup(hass, options={OPT_SEFF_ENABLED: True})
    hass.states.async_set(SOLAR_RADIATION, str(GHI))
    hass.states.async_set(
        "sun.sun", "above_horizon", {"azimuth": 270.0, "elevation": 34.0}
    )
    # covers never given a state -> current_position None -> assume-open + degraded

    state = build_house_state(
        hass, entry, entry.runtime_data, base_covers=PADRONALE_COVERS
    )
    assert state.zones["main_bedroom"].s_eff_source == SEFF_SOURCE_DEGRADED


# --- units seam + migration (STORY_SEFF §4, §7.6–7) -----------------------------


from datetime import datetime, timedelta  # noqa: E402
from dataclasses import replace  # noqa: E402

from custom_components.villa_hvac.const import (  # noqa: E402
    COOL_GAIN_SOLAR,
)
from custom_components.villa_hvac.policies import ThermalEstimator  # noqa: E402
from custom_components.villa_hvac.supervisor import (  # noqa: E402
    HouseState,
    ReturnRoom,
    ZoneSnapshot,
    house_load_index,
    rebase_solar_units,
    return_lead_time,
    seed_params,
)

T0 = datetime(2026, 7, 4, 2, 0, 0)


def _est_leader(s_eff, source=SEFF_SOURCE_GHI, **kw):
    base = dict(
        zone_id="lr", name="lr", climate="climate.lr", emitter="fancoil",
        fancoil_units=(("fan.lr", "switch.lr_man"),),
        s_eff=s_eff, s_eff_source=source,
    )
    base.update(kw)
    return ZoneSnapshot(**base)


def _est_state(z, *, now, solar=0.0):
    return HouseState(
        now=now, zones={z.zone_id: z}, outdoor_temp=30.0, solar=solar,
        consenso_freddo="off", blocco="off",
    )


def test_rebase_wipes_b_only():
    p = seed_params(0.02, 0.004, 0.1, 0.9, p0_passive=(0.5, 1e-5, 4.0), p0_k=4.0)
    p = replace(
        p, p=(0.1, 0.01, 0.02, 0.01, 5e-6, 0.03, 0.02, 0.03, 2.0),
        n=200, n_k=30, p_k=1.5, s_hi=900.0,
    )
    r = rebase_solar_units(p, prior_b=COOL_GAIN_SOLAR, p0_b=1e-5)
    assert r.b == COOL_GAIN_SOLAR and r.s_hi == 0.0
    assert r.p == (0.1, 0.0, 0.02, 0.0, 1e-5, 0.0, 0.02, 0.0, 2.0)
    assert (r.a, r.c, r.k, r.n, r.n_k, r.p_k) == (p.a, p.c, p.k, p.n, p.n_k, p.p_k)


def test_ensure_units_rebases_once_and_clears_buffer():
    est = ThermalEstimator()
    est.params["lr"] = replace(est._prior(), b=0.005, s_hi=800.0, n=50)
    est._buf["lr"] = [(T0, 24.0, 30.0, 500.0, None, False)]  # OLD-units samples
    est._last_w["lr"] = False

    est.ensure_units("lr", "seff1:292x1")
    assert est.params["lr"].b == COOL_GAIN_SOLAR and est.params["lr"].s_hi == 0.0
    assert est.params["lr"].n == 50
    assert "lr" not in est._buf and "lr" not in est._last_w
    assert est._s_units["lr"] == "seff1:292x1"

    snap = est.params["lr"]
    est.ensure_units("lr", "seff1:292x1")   # same tag -> no-op
    assert est.params["lr"] == snap


def test_ensure_units_cover_count_change_rewipes():
    est = ThermalEstimator()
    est.ensure_units("lr", "seff1:292x1")
    est.params["lr"] = replace(est._prior(), b=0.003, s_hi=400.0)
    est.ensure_units("lr", "seff1:292x2")   # second cover on the SAME facade
    assert est.params["lr"].b == COOL_GAIN_SOLAR and est.params["lr"].s_hi == 0.0


def test_ensure_units_symmetric_flag_off_rewipes():
    est = ThermalEstimator()
    est.ensure_units("lr", "seff1:225x1")
    est.params["lr"] = replace(est._prior(), b=0.0025, s_hi=600.0)
    est.ensure_units("lr", SEFF_UNITS_GHI)  # facade -> ghi (flag off / labels gone)
    assert est.params["lr"].b == COOL_GAIN_SOLAR and est.params["lr"].s_hi == 0.0


def test_units_tag_dump_load_roundtrip_and_missing_tag_is_ghi():
    est = ThermalEstimator()
    est.load({"lr": {"a": 0.1, "b": 0.001, "c": 0.0, "k": 0.9, "p": [0.0] * 9,
                     "p_k": 0.0, "n": 5}})
    assert est._s_units["lr"] == SEFF_UNITS_GHI  # pre-STORY_SEFF row -> GHI
    est._s_units["lr"] = "seff1:292x1"
    est2 = ThermalEstimator()
    est2.load(est.dump())
    assert est2._s_units["lr"] == "seff1:292x1"


def test_estimator_skips_fallback_and_degraded_sources():
    for source in (SEFF_SOURCE_FALLBACK, SEFF_SOURCE_DEGRADED):
        est = ThermalEstimator()
        for m in range(0, 17, 2):
            z = _est_leader(500.0, source=source, temp=24.0 + 0.1 * m, demand=False)
            est.observe(_est_state(z, now=T0 + timedelta(minutes=m)))
        assert est.params["lr"].n == 0, source  # units-impure -> zero updates


def test_estimator_gap_guard_restarts_window():
    est = ThermalEstimator()
    # 8 min of samples, a 10-min unobserved gap, then 14 more minutes: the
    # window must NOT complete across the gap (total span 32 min >= 15)...
    for m in (0, 2, 4, 6, 8, 18, 20, 22, 24, 26, 28, 30, 32):
        z = _est_leader(0.0, temp=24.0 + 0.05 * m, demand=False)
        est.observe(_est_state(z, now=T0 + timedelta(minutes=m)))
    assert est.params["lr"].n == 0
    # ...and completes once the POST-gap samples alone span >= 15 min.
    z = _est_leader(0.0, temp=25.7, demand=False)
    est.observe(_est_state(z, now=T0 + timedelta(minutes=34)))
    assert est.params["lr"].n == 1


def test_estimator_ingests_zone_s_eff_not_house_ghi():
    """Mutation pin: the buffered solar sample is z.s_eff — s_hi (max window-mean
    solar) lands at the ZONE value, not the house GHI."""
    est = ThermalEstimator()
    for m in range(0, 17, 2):
        z = _est_leader(300.0, temp=24.0 + 0.1 * m, demand=False)
        est.observe(_est_state(z, now=T0 + timedelta(minutes=m), solar=900.0))
    assert est.params["lr"].n >= 1
    assert est.params["lr"].s_hi == 300.0   # not 900


def test_return_lead_time_uses_room_s_eff():
    kw = dict(temp=27.0, target=24.0, a=0.03, b=0.0008, c=0.0, k=1.2)
    lit = ReturnRoom(s_eff=600.0, **kw)
    dark = ReturnRoom(**kw)  # falls back to the shared house solar (0)
    lead_lit = return_lead_time(
        [lit], 30.0, 0.0, max_lead=timedelta(hours=12), margin=timedelta(0)
    )
    lead_dark = return_lead_time(
        [dark], 30.0, 0.0, max_lead=timedelta(hours=12), margin=timedelta(0)
    )
    assert lead_lit > lead_dark  # the sunlit room cools slower -> longer lead


def test_house_load_index_uses_zone_s_eff():
    def _state(s_eff):
        # k-converged model: g_house aggregates eligible (k-trusted) zones only
        z = _est_leader(
            s_eff, temp=26.0, model_a=0.03, model_b=0.0008, model_c=0.0,
            model_k=1.2, model_k_confidence=0.9,
        )
        return HouseState(now=T0, zones={"lr": z}, outdoor_temp=30.0, solar=0.0)

    hot = house_load_index(
        _state(600.0), default_a=0.03, default_b=0.0008, default_c=0.0,
        default_capacity=1.2, k_conf_min=0.5,
    )
    dark = house_load_index(
        _state(0.0), default_a=0.03, default_b=0.0008, default_c=0.0,
        default_capacity=1.2, k_conf_min=0.5,
    )
    assert hot.g_house > dark.g_house  # the b·S term reads the zone's own S_eff


# --- §7.8 v0.41.0 non-regression pins (RUN fan law under facade S_eff) ----------


from custom_components.villa_hvac.supervisor import run_fan_pct  # noqa: E402

_LAW = dict(
    a=0.03, b=0.0008, c=0.0, k=1.2, pulldown=0.3, pulldown_hours=2.0,
    run_floor=20, fan_min_pct=0, band=1.5,
)


def _pct(temp, solar, *, at_peak=False):
    return run_fan_pct(
        temp=temp, outdoor=29.0, solar=solar, center=24.0, at_peak=at_peak, **_LAW
    )


def test_covers_open_afternoon_sizes_at_least_ghi():
    # (i) open west+south at 16:00: S_eff (1.44x GHI) must size >= the GHI answer
    assert _pct(24.2, 864.0) >= _pct(24.2, 600.0)


def test_shaded_above_band_at_peak_still_full_fan():
    # (ii) shaded (S_eff 0.3x GHI) + above band + peak latch -> 100% backstop
    assert _pct(25.0, 180.0, at_peak=True) == 100
    assert _pct(25.0, 180.0) < 100  # the law alone would size lower


def test_shaded_above_band_within_one_level_of_ghi():
    # (iii) shaded + temp >= center+B/2, below the latch: the stored-heat term
    # keeps the law-sized fan within one level of the GHI-sized answer
    assert abs(_pct(25.0, 180.0) - _pct(25.0, 600.0)) <= 10


def test_shaded_near_center_may_size_lower():
    # (iv) ACCEPTED (STORY_SEFF §7.8): behind a shaded cover with the room near
    # center, the glass gain IS lower -> the fan may size below the GHI answer;
    # the RUN floor still holds (a 0% RUN is self-defeating).
    shaded, ghi = _pct(24.1, 180.0), _pct(24.1, 600.0)
    assert shaded < ghi
    assert shaded >= _LAW["run_floor"]
