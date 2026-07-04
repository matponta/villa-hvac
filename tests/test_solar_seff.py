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
