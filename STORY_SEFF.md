# STORY_SEFF — Per-facade effective solar input (S_eff)

**Status: REVIEWED SPEC (3 independent designs → synthesis → 3-lens adversarial review,
18 confirmed findings folded in; 2026-07-04). Owner ask 2026-07-03, split from the
v0.41.0 defect train.**

Replace the thermal model's solar regressor input `b·S_ghi` with `b·S_eff`, where `S_eff` is a
per-zone effective irradiance **computed** (never learned) from sun geometry, facade normals
(from cover orientation labels), and live cover position. Same 4 learned params `{a,b,c,k}` —
S_eff is purely an input transform. `k` stays constant (water-side): the apparent "k(t)"
degradation at peak is G error — GHI falls in late afternoon exactly while the SW/WNW facade
beam gain peaks. Learned `b` is wiped-to-prior on any units change.

**Why it works (the phase fix):** GHI is a `sin(el)` bell peaking ~13:20 local; the real SW
facade (label "south", normal 225°) peaks ~16:30–17:30 and the WNW facade (label "west", 292°)
~17:30–19:00, *while GHI is already falling*. Under the GHI regressor, RLS fits `b` to the
day-average phase, so late-afternoon G is systematically underestimated for exactly the rooms
verified gain-limited at the 16:00 peak (padronale west+south, studio_v south, office west) —
which the RUN fan then undersizes, reading like k decaying. S_eff restores the phase.
Known residual: below ~8° sun elevation the rb clamp (§1.1) still under-states the true WNW
beam ~2× for the last ~45 min before the 3° cutoff — a deliberate noise/physics trade;
calibrate live-validation expectations accordingly.

---

## Resolved disagreements (synthesis round; decisions + one-line rationale)

| # | Disagreement | Decision | Why |
|---|---|---|---|
| R1 | f_geom form: physical beam tilt `cos(el)·cosΔ/sin(el)` clamped vs bounded `D+(1−D)·cos(el)·cosΔ` | **Physical tilt factor, clamped** | The `1/sin(el)` amplification IS the late-afternoon phenomenon this feature exists to model; the bounded form caps facade gain at GHI and re-suppresses exactly the evening WNW peak. |
| R2 | rb clamp 3.5 vs 3.0 | **3.0** (per facade AND on the zone beam sum, §1.3) | Conservative bound on the grazing-sun divergence; keeps zone S_eff ≤ 2.69×GHI, making R3's no-rescale decision safe. |
| R3 | Rescale MODEL constants (b prior, MAX_B, P0, excitation) vs keep | **Keep all unchanged** | With the tilt factor S_eff stays GHI-scale (§1.6), so `°C/h per W/m²` semantics survive. |
| R4 | No-cover-zone fallback: GHI identity vs computed neutral facade | **GHI identity, source `"ghi"`** | b is per-room, so per-row units are sound (the units tag prevents mixing); a guessed geometry violates the verified-facts rule and silently poisons b; GHI keeps those rooms byte-identical to today. |
| R5 | Store migration: version bump + migrate func vs per-row tag | **NO version bump; per-row units tag** | A v2 store makes a HACS rollback to v0.41 raise on load → `{}` → ALL learning (a, c, k included) nuked to priors; the tag keeps rollback learning with a bounded, safe-direction degradation (verified: old `load()` reads known keys and ignores extras). |
| R6 | Tag granularity | **Facade cover-multiset encoding** `"ghi"` \| `"seff1:<normal>x<count>,…"` (§1.5) | Any change to a fitted zone's aperture composition (new facade OR a second cover on an existing facade — it changes the per-facade mean g scale) must re-wipe b; only the multiset detects both. *(Review-amended: normals-only missed the same-facade case.)* |
| R7 | Wipe timing: lazy at first observe vs at `load()` | **Engine-driven, at the consumption seam, every cycle** (§4.2) | *(Review-amended, 2 CRITICAL findings)*: the observe path is unreachable with learning off or data missing, while `model_for` feeds control unconditionally — the rebase must be unconditional per cycle, upstream of consumption. |
| R8 | Flag vs always-on | **Flag `OPT_SEFF_ENABLED`, default OFF — structurally dark until the final slice** (§8) | The units tag makes every flip a symmetric wipe-to-prior (never mixed units); opt-in matches house rollout style. *(Review-amended: the toggle is not exposed/honored until every consumer is switched — enforced in code, not release notes.)* |
| R9 | Sun az/el missing on a facade zone | **(value, source) split**: control uses the GHI value, estimator skips | Control degrading to GHI over-states gain → fan sized up (comfort-safe); the estimator skip keeps b's units pure. |
| R10 | East/north normals: derive as complements vs exclude | **Exclude** | The verified normals are NOT orthogonal (225° vs 292° differ 67°) — the villa's rotation is non-uniform, so complements are not derivable; unmapped labels drop the cover from the facade set (harmless today: no east/north-labeled cover exists). |
| R11 | Diffuse constants | **`DIFFUSE_FRACTION=0.25`, beam weight 0.75, `DIFFUSE_VERTICAL=0.22`** | Including the ground-albedo term (0.2×0.5 view factor) is more physical for a vertical pane, and the higher floor preserves morning diffuse excitation on the WNW rooms (helps b identifiability). |
| R12 | Multi-cover composition | **Group by orientation; MEAN transmission within a facade; SUM across facades; beam sum clamped** (§1.3) | Two covers on one facade shade one aperture set (raw summing double-counts); summing across facades is the owner spec (main_bedroom = west + south). *(Review-amended: the beam SUM is additionally clamped at `SEFF_RB_MAX` — per-facade clamps alone allowed ~4.9×GHI when a low sun sits between the two normals.)* |

---

## 1. The one pure law — new module `supervisor/solar.py` (Q1, Q5)

Pure (math + frozen dataclasses only, zero HA imports — C2 house rule), re-exported from
`supervisor/__init__.py`. This is the ONLY place the S_eff formula exists; estimator ingest,
live band (trio+fold), planner sim, PV ranking, return-home, and diagnostics all consume its
output and never re-derive it.

```python
# supervisor/solar.py — constants (pure; const.py imports HA so they live here)

# Facade normals, compass deg (0=N). VERIFIED ONLY — the villa's rotation is
# non-uniform (225 vs 292 differ 67°), so east/north are NOT derivable as
# complements. A cover whose orientation label is absent here is EXCLUDED from
# the facade set (its zone may then fall back to GHI).
SEFF_FACADE_NORMALS: dict[str, float] = {
    "south": 225.0,   # verified live: "south" label = real SW facade
    "west": 292.0,    # verified live: "west" label = real WNW facade
}

SEFF_DIFFUSE_FRACTION   = 0.25  # fixed GHI beam/diffuse split (clear-summer)
SEFF_DIFFUSE_VERTICAL   = 0.22  # diffuse-on-vertical: sky view 0.5×0.25 + ground albedo 0.2×0.5
SEFF_BEAM_MIN_ELEVATION = 3.0   # deg: below this the beam term is 0 (1/sin ill-conditioned)
SEFF_RB_MAX             = 3.0   # clamp on the vertical tilt factor (per facade AND zone beam sum)
SEFF_COVER_FLOOR        = 0.2   # owner-fixed: fully-closed cover still transmits ~20%
SEFF_UNITS_GHI          = "ghi" # units-tag values (see §4)
SEFF_UNITS_PREFIX       = "seff1:"

# Source-quality values for ZoneSnapshot.s_eff_source (§3):
SEFF_SOURCE_FACADE   = "facade"           # full geometry, learnable
SEFF_SOURCE_DEGRADED = "facade_degraded"  # a cover position unknown → g=1 assumed; NOT learnable
SEFF_SOURCE_GHI      = "ghi"              # no facade / flag off → identity; learnable (GHI units)
SEFF_SOURCE_FALLBACK = "fallback"         # sun az/el or GHI missing; NOT learnable
```

### 1.1 `facade_beam_factor(sun_el_deg, sun_az_deg, normal_deg) -> float`

Direct-beam tilt factor for **vertical glazing** (R_b specialised to tilt 90°):

```
if sun_el <= SEFF_BEAM_MIN_ELEVATION: return 0.0
cos_i = cos(rad(sun_el)) * cos(rad(sun_az - normal_deg))
if cos_i <= 0: return 0.0                 # sun behind the facade
return min(cos_i / sin(rad(sun_el)), SEFF_RB_MAX)
```

Derivation: with one horizontal pyranometer, DNI ≈ GHI·(1−f_d)/sin(el); beam on the vertical
pane = DNI·cos(el)·cos(Δaz). The `1/sin(el)` is the physics of the evening problem — a low sun
square-on a WNW pane delivers more per m² than the horizontal sensor reads — and is exactly
what must be clamped: at el→0 sensor noise and the fixed split amplify without bound. Beam is
zeroed below 3° elevation and rb capped at 3.0 (air-mass attenuation, frame shading, and glass
reflectance at grazing incidence make larger values unphysical for windows).

### 1.2 `cover_transmission(position: int | None) -> float`

HA position 0 = fully closed/down, 100 = open (verified). `None` (unknown this cycle) → **1.0**
(assume open): over-states gain → fan sized up → the comfort-safe direction; never treat None
as closed (that would starve the fan exactly while shading is active). A position-None aperture
additionally degrades the zone's source to `"facade_degraded"` (§1.3) so the estimator skips
the sample — the assume-open bias is safe for control but systematically fits b LOW if learned
(position dropouts correlate with shading hours).

```
g = SEFF_COVER_FLOOR + (1 - SEFF_COVER_FLOOR) * clamp(position, 0, 100) / 100
```

Linear is deliberate: no photometric data justifies a curve; the learned per-room `b` absorbs
the scale — only monotonicity and the owner-fixed 0.2 floor are load-bearing. `blocked` covers
(manual shade override) still contribute with their real `current_position` — blocked means
"don't actuate", not "no glass".

### 1.3 `zone_effective_solar(ghi, sun_el, sun_az, apertures) -> tuple[float | None, str]`

```python
@dataclass(frozen=True)
class Aperture:
    """One glazed facade of a zone: outward normal + live cover transmission."""
    normal_deg: float
    transmission: float   # mean cover_transmission() over the facade's covers
    degraded: bool = False  # any of the facade's cover positions was None this cycle

def zone_effective_solar(
    ghi: float | None, sun_el: float | None, sun_az: float | None,
    apertures: tuple[Aperture, ...],
) -> tuple[float | None, str]:      # (S_eff W/m²-eq, source)
    if ghi is None or not isfinite(ghi): return (None, SEFF_SOURCE_FALLBACK)
    if not apertures: return (ghi, SEFF_SOURCE_GHI)     # no-facade zone: GHI identity
    source = SEFF_SOURCE_DEGRADED if any(ap.degraded for ap in apertures) else SEFF_SOURCE_FACADE
    if sun_el is None or sun_az is None or not both finite:
        return (ghi, SEFF_SOURCE_FALLBACK)              # control uses, estimator skips
    if sun_el <= 0: return (0.0, source)                # night: exact zero
    beam_sum = sum(ap.transmission * facade_beam_factor(sun_el, sun_az, ap.normal_deg)
                   for ap in apertures)
    beam_total = min(beam_sum, SEFF_RB_MAX)             # zone-level clamp (review-amended)
    diffuse_total = sum(ap.transmission * SEFF_DIFFUSE_VERTICAL for ap in apertures)
    return (ghi * ((1.0 - SEFF_DIFFUSE_FRACTION) * beam_total + diffuse_total), source)
```

- **Diffuse floor**: a facade the beam has left still gains `0.22·g·GHI` — S_eff never zeroes
  while the sky is bright, so a bright shaded morning still excites `b`. The diffuse term stays
  in S_eff (not folded into `c`) because it is GHI-correlated, not constant — folding it into
  `c` would alias cloudy-vs-clear days into the intercept. `a(T_out−T)` keeps opaque-envelope
  conduction; `c` keeps constant internal gains.
- **Fixed split, not clearness-derived**: with one pyranometer and no DHI, an Erbs-style split
  adds an unverifiable branch; per the F4a-v2 philosophy only the SHAPE must be right — the
  per-room learned `b` absorbs the level. Overcast days over-state beam, but GHI is then small.
- **Multi-facade sum** (owner spec): main_bedroom = west(292°, g from grande_camera) +
  south(225°, g from piccola_camera). Its S_eff can exceed GHI in the afternoon when both beams
  land — physical (two glazed walls); the per-room `b` normalises total aperture area. The
  BEAM sum is clamped at `SEFF_RB_MAX` so a low sun between the two normals (az ~258°, el 8–15°,
  reachable on clear September evenings — both per-facade rb at the clamp) cannot push the zone
  to ~4.9×GHI; the hard bound is `0.75·3.0 + n_facades·0.22·g ≤ 2.69×GHI` for 2 facades.
- Not normalised to a mean: a mean would change an already-fitted room's units when a cover is
  added (the units tag re-wipe handles the addition instead).

### 1.4 `zone_solar_curves(ghi_curve, elevations, azimuths, apertures_by_zone, live_ratio) -> dict[str, list[float]]`

Per-step future S_eff for the planner: element-wise `zone_effective_solar(...)[0]` per zone,
with **cover positions frozen at their current values over the horizon** (v1 simplification;
under the v0.41.0 never-raise shading invariant positions mostly ratchet DOWN during the day,
so hold-current under-states future shading → over-states gain → comfort-safe). Fallback-source
steps use the GHI step value.

**Flat-mode consistency (review-amended):** when elevations/azimuths are empty (flat solar
model: `DEFAULT_SOLAR_FORECAST` off, astral missing, or the runtime exception fallback — which
can degrade ANY cycle), the planner must NOT silently sim on the house curve while actuation
runs on S_eff. Each zone's flat curve = `ghi_curve × live_ratio[zone]`, where
`live_ratio = s_eff_now / ghi_now` (current live geometry propagated; only when both known and
`ghi_now > 0`, else the house curve). Room plans / center schedule gain a `solar_domain`
marker (`"seff"` | `"ghi"`) so a domain divergence is visible in diagnostics, never silent.

### 1.5 `units_tag(apertures_with_counts) -> str`

`"ghi"` when the aperture set is empty (or the feature flag is off), else
`"seff1:" + ",".join(f"{int(normal)}x{count}" sorted by normal)` where `count` = number of
covers on that facade — e.g. `"seff1:225x1"` (studio_v), `"seff1:292x1"` (office),
`"seff1:225x1,292x1"` (main_bedroom). This is the per-row semantics stamp §4 compares.
**The cover multiset is load-bearing (review-amended):** a second cover appearing on an
already-fitted facade changes the per-facade MEAN transmission scale, so it must flip the tag
and re-wipe b — normals-only encoding missed it (a real hazard here: a KNX duplicate-entity
fix already happened once in this install).

### 1.6 Daily-shape sanity (pins the scale claims)

At ~45°N midsummer, open cover (g=1), single facade `f = 0.75·rb + 0.22` (az/el pairs
astronomically consistent — review-corrected; generate the test fixture from astral, not by
hand):

| local time | sun az/el | GHI | studio_v (225°) S_eff | office (292°) S_eff |
|---|---|---|---|---|
| 09:00 | ~100°/43° | ~550 | ~120 (diffuse only) | ~120 (diffuse only) |
| 13:20 noon | ~180°/68° | ~950 | ~410 | ~210 (beam behind) |
| 16:30 | ~255°/47° | ~600 | ~500 | ~470 |
| 17:45 | ~270°/34° | ~380 | ~380 | ~475 |
| 19:15 | ~295°/8° | ~100 | ~205 (rb 2.4) | ~250 (rb clamped) |

Single-facade f_geom ∈ [0, 0.75·3.0+0.22 = 2.47]; realistic single-facade peaks 0.6–1.7×GHI;
main_bedroom's two-facade sum is bounded by the zone beam clamp at **≤ 2.69×GHI** (its 17:30
open-cover value ≈ 2.56×GHI is real physics: two glazed walls both in beam). Same order as
GHI ⇒ no b-bounds rescale (§2). Day-scan pin asserts the ≤2.69 bound, not the draft's false
<2.4.

---

## 2. Units and knock-on constants (Q2)

S_eff stays **W/m²-equivalent, GHI scale** (§1.6). Therefore in `const.py` (verified current
values at lines 557/585/591/609):

- `COOL_GAIN_SOLAR = 0.0008` (b prior) — **unchanged**.
- `MODEL_MAX_B = 0.01` — **unchanged** (live learned b today is 0.00014–0.00038 across rooms —
  26–70× headroom; a room gaining 1.5 °C/h at S_eff 600 needs b=0.0025 — 4× headroom).
- `MODEL_P0_PASSIVE = (0.5, 1e-5, 4.0)` — **unchanged** (regressor scale unchanged).
- `MODEL_SOLAR_EXCITATION_MIN = 150.0` — **unchanged numerically; semantics improve**: `s_hi`
  (thermal.py:45,137) becomes max window-mean **S_eff**, so "b identified" now means *this
  room's own glass* was excited (a bright diffuse hour gives ~0.22·700 ≈ 154; a beam hour ≫150;
  a facade room whose covers are always closed correctly stays un-identified). The migration
  wipe resets `s_hi` (§4) so `abc_identified`/`planner_eligible` re-gate in the new units.
  **Live impact (verified 2026-07-04): 5 rooms are planner_eligible today** (padronale,
  studio_v, salotto, gabriele, sala_giochi; s_hi=980 GHI-units) — the wipe TEMPORARILY
  de-eligibles the facade rooms until S_eff re-excites (days). Accepted: `unified_planner` is
  deploy-dark/OFF and its enable is already gated on a soak; flag-on must simply precede any
  Phase-7 co-enable so eligibility re-gates in S_eff units.

New in `const.py`: `OPT_SEFF_ENABLED = "seff_enabled"`, `DEFAULT_SEFF_ENABLED = False`,
coerced in `SupervisorConfig.from_options()` (C3) — **AND `SEFF_CONSUMERS_READY: bool`, a
code-level constant `from_options` ANDs into the flag** (§8, review-amended): False until the
release where every consumer is switched, so a half-migrated tree can never run S_eff even if
the option key is set. All SEFF_* physics constants live in `supervisor/solar.py` (pure —
const.py imports homeassistant).

---

## 3. Missing-data fallbacks (Q3)

`zone_effective_solar` returns `(value, source)`; the source tag is the consistency mechanism.
**Control consumers always use the value** (facade or degraded); **the estimator only learns
from `"facade"` and `"ghi"` sources** — `"fallback"` and `"facade_degraded"` samples are
skipped (§4/§6 row 1).

| Condition | S_eff | source | notes |
|---|---|---|---|
| GHI (`state.solar`) None | None | `"fallback"` | today's behavior: estimator skips; `cooling_load` drops the b·S term |
| Zone with no mapped facade (living_room+kitchen, gabriroom, sala_giochi, rack/stairs_p1 today) | = GHI | `"ghi"` | its b keeps GHI semantics — no migration, no wipe, byte-identical to today (R4) |
| Facade zone, sun az/el None/non-finite | = GHI | `"fallback"` | control over-states gain (safe); estimator skips WITHOUT clearing the buffer (a sun.sun dropout is brief; the gap guard below covers long ones) |
| Cover `current_position` None | that cover g=1.0 | `"facade_degraded"` | assume open — errs high → fan oversizes (safe for CONTROL); estimator skips (learning it would fit b systematically LOW — review-amended) |
| Sun elevation ≤ 0 | 0.0 | `"facade"`/`"facade_degraded"` | GHI≈0 anyway; exact zero keeps night windows clean for {a,c} |
| Cover orientation label ∉ `SEFF_FACADE_NORMALS` | cover excluded from the facade set | — | if that empties the set → GHI identity (R10) |
| Winter | no special-casing | — | the geometry law is season-agnostic; low winter sun raises rb on the SW facade (clamped), which is the real winter-gain physics and helps future #7 radiant pre-heat |

**Estimator gap guard (review-amended, fixes a pre-existing hazard too):** skipped samples
(fallback/degraded/transient/None-input) leave the buffer intact, so a window could silently
span an unobserved interval containing a chilled-water stint (e.g. a sun.sun outage across a
valve open/close → the stint's cooling lands inside a "passive" window). On appending a sample,
if `now − last_sample > MODEL_GAP_MAX_S` (180 s ≈ 6 cycles — singles/40 s KNX blips pass), clear
the buffer first: a window may never bridge a gap it didn't observe.

One deliberate NON-change: do **not** clear the estimator buffer on a cover move mid-window — a
g step is statistically identical to a cloud step, which the 15-min window mean already
tolerates; it is legitimate regressor excitation.

---

## 4. Migration — `RoomModelStore` + `ThermalEstimator` (Q4)

**No Store version bump** — `Store(hass, 1, "villa_hvac_room_models")` (engine.py:281) stays
v1 (R5). Migration is a per-row **units tag** (§1.5) + an **engine-driven rebase at the
consumption seam** (R6, R7 as amended).

### 4.1 Tag

- `ThermalEstimator.dump()` (policies.py:744) adds per-zone `"s_units": <tag>` (§1.5 format).
- `load()` (policies.py:724) reads `d.get("s_units", "ghi")` into a new
  `ThermalEstimator._s_units: dict[str, str]` — **rows without the key are `"ghi"` by
  construction** (they were fitted to GHI). Existing per-row try/except + physicality
  validation (finite, b≥0, k>0, len(p)==9) unchanged.

### 4.2 Rebase — `ThermalEstimator.ensure_units(zone_id, tag)` at the consumption seam

**(Review-amended — was "lazy at first observe", which 2 CRITICAL findings killed: `observe()`
hard-returns when `model_learning_enabled` is False (policies.py:647) and `_observe_zone` has
data-availability early-returns, while `model_for` feeds ZoneSnapshot.model_* → live fan sizing
UNCONDITIONALLY (engine.py:443–448) — GHI-fitted b would drive S_eff-fed control indefinitely.)**

The engine calls `thermal.ensure_units(zid, zone_tag)` for EVERY cooling leader EVERY cycle,
immediately before `model_for(zid)` populates the snapshot — independent of
`model_learning_enabled`, of temp/valve availability, and of the estimator's observe path.
`zone_tag` is what the engine just computed for the zone this cycle (`"ghi"` when the flag is
off). `ensure_units` is a cheap string compare when tags match; on mismatch it:

1. applies the pure `thermal.rebase_solar_units(params, prior_b, p0_b)` (below) when stored
   params exist,
2. records the new tag in `_s_units[zid]`,
3. **clears the zone's sample buffer + `_last_w`** (review-amended: the buffer holds per-sample
   solar in the OLD units; the window MEAN of a mixed buffer would land as the first — maximum
   covariance gain, near-fully-trusted — b update, ±50–75% of the prior in one shot),
4. logs INFO (`zone, old_tag → new_tag`).

Pure `rebase_solar_units(params, *, prior_b, p0_b) -> ThermalParams` (in `supervisor/thermal.py`):

- `b := prior_b` (= `COOL_GAIN_SOLAR`) — wipe-to-**prior**, never 0: `blend_params` applies the
  shared count weight `wa = n/(n+MODEL_ABC_CONF_MIN)` to a, b, c jointly, so with high kept `n`
  a zeroed b would be *trusted*; reset-to-prior makes blended b == prior exactly on day 0 while
  RLS re-identifies under the reopened variance.
- Covariance: reopen b's row/col only in the 3×3 row-major p — `p[1]=p[3]=p[5]=p[7]=0.0`
  (a–b, b–c cross terms), `p[4] = MODEL_P0_PASSIVE[1]` (fresh b variance). Keep
  `p[0], p[2], p[6], p[8]` (a, c variances + a–c cross): the textbook single-parameter reset —
  the RLS gain ∝ P·x routes prediction error almost entirely into b while a, c stay pinned.
- `s_hi := 0.0` — it measured excitation of the OLD regressor; keeping it would fake
  `abc_identified`/`planner_eligible` on a wiped b. (Live consequence stated in §2.)
- **Keep `a, c, k, n, n_k, p_k`**: their regressors `(T_out−T, 1)` and the water-side capacity
  did not change; k's historical windows used the then-current G (not re-derivable), and the
  apparent k sag was G error this feature removes going forward. (Alternative `n := min(n, 40)`
  decay rejected: it would also distrust the well-learned a and c.)

Rebase triggers on ANY tag mismatch, both directions: ghi→facade (migration / flag-on / new
cover labeled), facade→ghi (flag-off / labels removed), facade-set OR cover-count change
(`seff1:225x1` → `seff1:225x1,292x1`, `seff1:292x1` → `seff1:292x2`). Unknown/future tag values
simply mismatch → rebase (keeps a, c, k — strictly better than dropping the row).

### 4.3 k-freeze during b re-identification (review-amended, 2 MAJOR findings)

`rls_capacity_update` computes `G = a·ΔT + b·S + c` from the LEARNED (unblended) passive row
(thermal.py:154). Post-rebase, `b == prior` in new units while held-fan w=True windows cluster
in high-S_eff afternoons — the G error is systematically signed, walking k while `n_k` inflates
its confidence, and `planner_eligible` could reopen on the corrupted pair.

**Rule: a w=True window may only update k when the zone's passive model is identified** —
`abc_identified(params)` (n-confidence ≥ 0.5 AND `s_hi ≥ MODEL_SOLAR_EXCITATION_MIN`). The
`s_hi := 0` reset then suspends k learning automatically from rebase until b has re-excited and
re-identified in the new units; no extra persisted state needed. While suspended: no k update,
no `n_k` increment. This is also the right general principle (k_obs is only as good as G) and
is a no-op for every zone today (all identified rooms keep learning k; office's k had never
learned while its emitter was out of service — emitter REPAIRED 2026-07-08, zone
re-enabled, so office now accrues k too once it runs cooling windows).

### 4.4 Rollback matrix (verified against the actual `load()`)

- **Old store + new code**: no `s_units` key → "ghi"; facade zones rebase b at the seam
  once the flag is on. a, c, k learning preserved. ✔
- **New store + old v0.41 code (HACS rollback)**: old `load()` reads the known keys —
  `s_units` is simply unread. Worst case: a facade-fitted b applied to GHI — same order of
  magnitude (S_eff is GHI-scale, R3), bounded by `MODEL_MAX_B`, prior-blended, pulled back by
  continued GHI learning; band + at-peak backstop own comfort. **No unsafe rollback state.** ✔
- **Roll forward again**: old code's `dump()` drops `s_units` → rows read as "ghi" → re-wipe to
  prior. Re-wiping is idempotent-safe and correct (old code had been refitting b in GHI units). ✔
- **Corrupt store**: `RoomModelStore.async_load` already swallows → `{}` → priors; per-row
  validation unchanged. ✔
- **Flag off→on→off**: symmetric tag flips → symmetric wipes → never mixed units. ✔

---

## 5. Placement + per-cycle engine feed (Q5)

- **Law**: `supervisor/solar.py` (§1), pure, re-exported from `supervisor/__init__.py`.
- **`ZoneSnapshot`** (`supervisor/model.py`) gains three fields (populated only for cooling
  leaders; followers/radiant stay defaults):

```python
s_eff: float | None = None    # per-zone effective irradiance this cycle (W/m²-eq)
s_eff_source: str = "ghi"     # "facade" | "facade_degraded" | "ghi" | "fallback"
s_eff_units: str = "ghi"      # stable semantics tag, e.g. "seff1:225x1,292x1" (§4)
```

  No `HouseState` change: `state.solar` REMAINS raw house GHI (shading policy + nowcast anchor
  keep it, Q9). No `CoverInfo` change (orientation/zone/current_position already carried,
  v0.41.0).
- **Engine, live cycle** (`build_house_state`, engine.py:368):
  1. Hoist the cover resolution + enrichment (currently after the zone loop, engine.py:480–508)
     and the `sun.sun` az/el + `_num(hass, SOLAR_RADIATION)` reads (509–510, 537) **above** the
     zone loop.
  2. New helper `_zone_apertures(covers) -> dict[area_id, tuple[Aperture, ...]]`: group enriched
     `CoverInfo` by `cover.zone` (== ZONES zone_id for the verified rooms; unmatched area_ids
     never match a zone — no crash, GHI fallback), map orientation through
     `SEFF_FACADE_NORMALS` (unmapped label → cover excluded), per facade mean
     `cover_transmission(current_position)` + degraded flag, one `Aperture` per facade.
  3. Per cooling leader: flag ON → `s_eff, src = zone_effective_solar(ghi, el, az, apertures)`,
     `s_eff_units = units_tag(...)`; flag OFF → `(ghi, "ghi", "ghi")` for every zone —
     **byte-identical to today**. The flag has exactly ONE switch site; every consumer reads
     `z.s_eff` unconditionally.
  4. `thermal.ensure_units(zid, s_eff_units)` runs here (§4.2), before `model_for` fills
     `model_*`.
  5. Engine caches `self.last_s_eff: dict[str, tuple[float | None, str]]` for the sensor.
  Cost: ~4 trig calls per facade per 30 s cycle — trivial.
- **Engine, planner horizon**: extend `_solar_forecast` (engine.py:1034) to also collect
  per-step **azimuths** (`from astral.sun import azimuth as _sun_azimuth`, same guarded import
  block as `elevation`, engine.py:150–158 — astral stays engine-only, supervisor purity
  preserved), returning `(ghi_curve, solar_model, elevations, azimuths)`; the flat-fallback
  paths return empty lists **and per-zone curves fall back to the live-ratio propagation
  (§1.4), never silently to the house curve**. New `solar_by_zone = zone_solar_curves(...)`
  computed at the three horizon call sites (`plan_view` build ~884,
  `_maybe_refresh_schedule` ~917, `_pv_bias_apply` ~1111) — the schedule cadence, NOT every
  30 s (cached-schedule discipline).

---

## 6. Consumer-by-consumer switch — every site pairing `b` with a solar value (Q6)

All verified against the repo. Every site must read the SAME per-zone S_eff or units silently mix.

| # | Site (file:line, verified) | Today | Change |
|---|---|---|---|
| 1 | Estimator ingest — `policies.py:656` (`solar = state.solar`), window means → `rls_passive_update` :702 / `rls_capacity_update` :717 | house GHI | `solar = z.s_eff`; **skip the sample** (without clearing the buffer) when `z.s_eff is None` or `z.s_eff_source not in ("facade", "ghi")`; gap guard §3 on append; k-freeze §4.3 gates the w=True branch. `s_hi` accrues in S_eff units. |
| 2 | Live band, **trio** — `policies.py:549` `run_fan_pct(..., solar=state.solar, ...)` | house GHI | `solar=z.s_eff` |
| 3 | Live band, **fold** — `policies.py:1079` same call | house GHI | `solar=z.s_eff` — verbatim-identical to the trio (differential harness pins it; both edits in one commit, BEFORE the P3 trio deletion so the harness still exists to pin them) |
| 4 | `house_load_index` — `supervisor/planner.py:202` `cooling_load(z.temp, state.outdoor_temp, state.solar, ...)` | house GHI | `z.s_eff` (internal fix covers both callers: engine plan_view and policies) |
| 5 | `simulate_room` — `planner.py:488` (`_solar_at(solar, i)` → cooling_load + run_fan_pct) | house curve | signature unchanged; the CALLER passes the zone's curve |
| 6 | `schedule_precool` — `planner.py:567` `g_peak = cooling_load(..., _solar_at(solar, ...))` | house curve | same: caller passes the zone curve |
| 7 | `build_room_plans` — `planner.py:598` | one `solar` list for all zones | gains `solar_by_zone: dict[str, list[float]] \| None = None`; per zone `solar_by_zone.get(zone_id, solar)` |
| 8 | `plan_center_schedule` — `planner.py:988` (effectiveness horizon), :976-area (schedule_precool call), :1039 (advisory g_sum on `measured.solar`), :1057–1058 (`return_lead_time(..., measured.solar)`) | house GHI/curve | gains `solar_by_zone`; effectiveness + precool per zone curve; g_sum uses `zz.s_eff`; return rooms carry per-room s_eff (#10) |
| 9 | PV effectiveness — `engine.py:1115–1123` `cooling_effectiveness` over `_house_cooling_model` (engine.py:1070, house-MEAN a,b,c,k) | mean-b × house curve | replace with per-zone loop: `eff[h] = mean over leaders of cooling_effectiveness(center, t_out, solar_by_zone.get(zid, curve)[h], zone params)`. Averaging b's with mixed per-zone units against one GHI curve is exactly the silent mix this table kills. `_house_cooling_model` becomes unused → delete. |
| 10 | `return_lead_time` — `supervisor/returnhome.py:59–74` (one solar for every room); live caller `returnhome.py` + planner :1057 | house GHI | `ReturnRoom` gains `s_eff: float \| None = None` (default keeps old constructors valid); the shared `solar` param becomes the per-room fallback; both callers fill `zz.s_eff` |
| 11 | hvac_model sensor — `sensor.py:365` `cooling_load(..., _num_state(SOLAR_RADIATION), ...)` | house GHI | read `engine.last_s_eff[zone]` (fallback GHI); attributes gain `s_eff`, `s_eff_source`, `s_units` |
| 12 | Shading policy — `policies.py:309–322` | house GHI | **unchanged (Q9)** |
| 13 | `solar_curve_v2` / nowcast anchor — `planner.py`, `engine.py:1027` | house GHI | **unchanged** — produces the house GHI curve; per-zone curves derive FROM it |

`control_law.cooling_load` / `run_fan_pct` / `cooling_effectiveness` **signatures unchanged** —
pure functions of whatever S they are handed; only call-site values change. `RoomParams` needs
no new fields.

---

## 7. Parity — one shared law, test-pinned (Q7)

Same pattern as the pinned `run_fan_pct` parity:

1. **Law singleton**: only `supervisor/solar.py` computes f_geom/g/S_eff; the mutation-style
   pins below make any divergent re-implementation fail.
2. **Estimator == trio == fold == snapshot**: build one HouseState with a facade zone
   (`state.solar ≠ z.s_eff`, e.g. closed west cover at noon); assert the estimator's buffered
   solar sample, the `solar=` kwarg both `run_fan_pct` call sites receive (spy), and
   `ZoneSnapshot.s_eff` are the identical float — AND that the fan % differs from the GHI-fed
   answer (mutation-proof the wiring actually switched, house "wiring pin" style).
3. **Differential harness**: extend the trio/fold generator with `s_eff`/`s_eff_source`
   fields; the harness (the P3 soak gate) must stay green with both paths reading `z.s_eff`.
4. **Planner step-0 anchor**: with `ghi_curve[0] == state.solar`, `elevations[0]/azimuths[0]`
   == live sun, positions == current: `zone_solar_curves(...)[zid][0] == pytest.approx(z.s_eff)`
   — plan and actuation share the law at the seam. **Also pinned in FLAT mode** via the
   live-ratio propagation (§1.4): step 0 still equals `z.s_eff` by construction; the
   `solar_domain` marker is asserted present.
5. **Geometry pins**: behind-facade → beam 0 (diffuse floor only); rb clamp engaged at grazing
   sun; el ≤ 3° → beam 0; el ≤ 0 → S_eff 0; GHI None → (None,"fallback");
   sun-None-with-apertures → (ghi,"fallback"); no apertures → (ghi,"ghi"); closed cover → 0.2×;
   g(None)=1 + source `"facade_degraded"`; **zone beam-sum clamp: main_bedroom two-facade
   day-scan ≤ 2.69×GHI, and the az≈258°/el 10° both-clamped case returns exactly the clamped
   sum, not ~4.9×**; two same-facade covers → mean g; WNW 17:30 S_eff > WNW 13:20 S_eff
   relative to GHI (the phase fix); `SEFF_FACADE_NORMALS["south"] ∈ SHADING_AZIMUTH_BANDS
   ["south"]` (135,270) and west ∈ (225,315) — cross-pin the two encodings of the villa
   rotation; **verified-room tag cross-check: units_tag == "seff1:225x1,292x1" (main_bedroom),
   "seff1:292x1" (office), "seff1:225x1" (studio_v)** (review-amended: the draft had office on
   225).
6. **Units-seam pins**: tag flip → exactly one `rebase_solar_units` (b→prior, s_hi→0,
   p b-row/col reset, a/c/k/n untouched) **+ buffer/`_last_w` cleared (no RLS update may
   consume samples buffered under the old tag)**; **`ensure_units` fires with
   `model_learning_enabled=False` and with temp/valve/solar missing** (the CRITICAL-finding
   pin); same-facade cover-count change re-wipes; dump→load round-trip; missing-tag row =
   "ghi"; full §4.4 rollback matrix incl. old-code-dump→roll-forward re-wipe and corrupt rows.
7. **Estimator-quality pins**: position-None window → zero passive updates
   (`"facade_degraded"` skip); sample gap > `MODEL_GAP_MAX_S` → window restarts (no window
   bridges an unobserved valve edge); **k-freeze: a held-fan w=True window between rebase and
   re-identification produces NO k update and NO n_k increment; k learning resumes once s_hi
   recrosses the excitation threshold with n-confidence held**.
8. **v0.41.0 non-regression pins (review-amended, split by cover state)**:
   (i) covers-OPEN padronale at 16:00 (S_eff ≥ GHI on west+south) → `run_fan_pct` ≥ the
   GHI-sized pct;
   (ii) covers-SHADED, above band, outdoor ≥ peak threshold → 100% via `peak_latch` (keys off
   outdoor temp, not solar — verified control_law.py:189);
   (iii) covers-SHADED, outdoor BELOW the latch (e.g. 28–29 °C): the law-sized fan MAY sit
   below today's GHI-sized answer — this is real physics (the glass gain IS lower behind a
   shaded cover) and is ACCEPTED, bounded by: the stored-heat term dominates whenever the room
   is meaningfully above center (a room at center+2° demands base+1.0 °C/h → ≥100% at measured
   k regardless of solar), the RUN floor 20%, and the band's model-free comfort guarantee. Pin
   the scenario: shaded + temp ≥ center+B/2 → pct within 1 level of the GHI-sized answer;
   shaded + temp near center → lower is allowed. Add the live watch item (§8).
   Plus: stored-heat `effective_pulldown` and the RUN floor untouched; flag-OFF byte-identity
   (golden-state differential vs v0.41.0 behavior).

---

## 8. Rollout — flag, slicing, validation (Q8)

**Flag `OPT_SEFF_ENABLED`, default OFF (R8) — structurally dark until the last slice:**
`SupervisorConfig.from_options` computes `seff_enabled = option AND SEFF_CONSUMERS_READY`;
the constant flips True (and the options-flow toggle appears) only in the release where EVERY
consumer in §6 is switched. Review finding: with the toggle live from slice one, an early
flag-on would run live control in S_eff units while PV effectiveness / the center-schedule
planner still consumed the house GHI curve — a real actuation-side mix (pv_bank/pv_coast move
the live band center) enforced only by release-note prose. Code, not prose.

Flag OFF → `build_house_state` populates `s_eff = state.solar, source="ghi", units="ghi"` for
every zone → every consumer computes byte-identically to today. Flag flips are safe by
construction: each direction triggers the symmetric units rebase (§4), never mixed units.

**NOT observer-only** — b feeds live fan sizing through the blend once confident — so
correctness rests on: (a) the single engine switch site (§5) + the per-row units tag +
`ensure_units` seam (§4) keep ingest and consumption in the same units always; (b) every
actuation-side error is bounded by b ∈ [0, MODEL_MAX_B], the stored-heat RUN sizing, the RUN
floor 20%, the at-peak 100% backstop, and the band's model-free comfort guarantee (all
v0.41.0, re-pinned §7.8). The estimator ticks deploy-dark, so b re-learns from day one of
flag-on even before actuation lights up.

**Release slicing** (small tagged increments; sequence the trio/fold edits BEFORE the P3
trio deletion so the differential harness still exists to pin them — naturally satisfied,
P3 is STOP-gated on the v0.41.0 soak):

- **vN — pure law + plumbing (inert)**: `supervisor/solar.py` + full geometry unit tests;
  `OPT_SEFF_ENABLED` in const + `SupervisorConfig` with `SEFF_CONSUMERS_READY = False` (no
  options-flow exposure); ZoneSnapshot fields; `build_house_state` feed (flag structurally
  OFF ⇒ GHI everywhere); `last_s_eff` cache; hvac_model sensor attributes (`s_eff`,
  `s_eff_source`, `s_units`). ZERO consumer changes. Owner watches diagnostics live a few
  days: studio_v S_eff must peak mid-afternoon and drop when its cover shades; padronale
  morning S_eff ≈ 0.22·GHI.
- **vN+1 — units seam + estimator + live control**: `s_units` in dump/load +
  `rebase_solar_units` + `ensure_units` engine seam + k-freeze + gap guard + migration-matrix
  tests; estimator ingest (source-gated skip); trio + fold `solar=z.s_eff`;
  `house_load_index`; `ReturnRoom.s_eff` + `return_lead_time`; sensor native_value; parity
  pins §7.2–3, §7.6–8. Flag still structurally OFF ⇒ s_eff==GHI ⇒ no behavioral change;
  differential harness green.
- **vN+2 — planner horizon + PV + light-up**: astral azimuth track in `_solar_forecast`;
  `zone_solar_curves` + flat-mode live-ratio; `solar_by_zone` through
  `build_room_plans`/`plan_center_schedule`/`simulate_room`/`schedule_precool` callers;
  per-zone PV effectiveness (delete `_house_cooling_model`); step-0 anchor §7.4 (incl. flat
  mode); hvac_plan `solar_domain` diagnostics; **flip `SEFF_CONSUMERS_READY = True` + add the
  options-flow toggle**.
- **Flip ON after vN+2 only** (owner, options flow). Flag-on must precede any Phase-7 /
  `switch.unified_planner` enable so eligibility re-gates in S_eff units (§2).

**Live validation gates (~1 week after flag-on)** (review-amended — the draft watched
`b_solar` "rising through the evening", but b is the RLS constant: it has no intra-day shape,
and sunny evenings are w=True windows producing zero passive updates):
(a) **G-phase fix**: the hvac_model sensor STATE (G = cooling_load with S_eff) on
    office/main_bedroom at 17:30–18:30 exceeds its 13:20 value on clear days — the inverse of
    today's GHI ordering. Equivalently the afternoon RUN fan sizes up on the west rooms
    16:00–19:00 without `peak_latch` being the reason.
(b) **b re-identification**: studio_v/office/padronale `s_hi` recrosses 150 in S_eff units and
    `b_solar` moves off the prior; day-to-day b drift shrinks vs the GHI era (slow gate).
(c) **k-freeze visible**: `capacity_updates` (n_k) holds constant from flag-on until (b), then
    resumes.
(d) **No shaded-afternoon comfort loss**: shaded west rooms at outdoor < 30 °C hold the band
    (the accepted §7.8(iii) fan reduction must not turn into temp loss; if it does, consider a
    solar-conditioned backstop or a lower `peak_latch` threshold — decide on soak data).
(e) padronale morning fan-on + at-peak 100% v0.41.0 behaviors unchanged.
Then `DEFAULT_SEFF_ENABLED = True` in a follow-up release.

**Re-identification expectation** (west-room worst case): passive windows come from nights/
mornings (diffuse S_eff ≈ 0.22·GHI) and band REST half-cycles on sunny afternoons (valve closed
by our own slam, ~1–1.5 h) — the high-S_eff excitation source; ~5–10 passive updates/day/room.
Since wiped b == prior and n is kept, blended b == prior from day 0 and tracks new updates at
the reopened-covariance rate: effectively re-identified in ~1–2 weeks of mixed weather, with
control behaving like the prior meanwhile. Comfort never depends on it.

**Identifiability × shading feedback** (checked): no classic closed-loop failure — the shading
policy's inputs (house GHI, sun geometry, user numbers) are exogenous to the passive
regression's error term (room temp / model estimates do not drive covers). Residual risks, both
bounded: a wrong linear-g is a scale error on b active only when shaded, anchored back by
unshaded windows; shading compresses top-end S_eff excitation → slower convergence, mitigated
by the diffuse floor + REST windows. (Two review findings claiming a permanent excitation
catch-22 were REFUTED — shading releases below 200 W/m² GHI and the diffuse floor keeps bright
hours ≥150 S_eff — but `s_hi` recovery is gate (b) of the live validation as cheap insurance.)

---

## 9. Shading policy unchanged (Q9)

`shading_policy` keeps triggering on house-level `state.solar` + `SHADING_AZIMUTH_BANDS` +
`proportional_shade_position` (policies.py:309–322), including the v0.41.0 never-raise
min(current,target), Via/Vacanza full-close, and the south band (135,270). S_eff is a model
INPUT transform only.

---

## NON-GOALS

- Shading decisions keyed off per-zone S_eff (changes verified live behavior — out of scope).
- Learning f_geom, the diffuse split, or the cover transmission curve (S_eff is COMPUTED; b
  absorbs scale — owner-fixed).
- Re-deriving or time-varying k (water-side constant — owner-fixed; the apparent sag was G error).
- Simulating the shading policy's future cover moves over the planner horizon (v1 holds current
  positions; comfort-safe under never-raise).
- East/north facade normals (unverified, non-derivable — R10); guessed facades for no-cover
  zones (R4).
- A clearness-index (Erbs) beam/diffuse split (unverifiable with one pyranometer).
- Per-facade b (one b per room stays; apertures sum).
- Any change to solar_curve_v2 / the nowcast anchor / free-cool / shading triggers (GHI domain).

## OPEN QUESTIONS (carry to live validation; none block implementation)

- Is `cover.zone` (HA area_id) == ZONES zone_id for every cooled leader as covers get labeled,
  or only the three verified rooms? Unmatched rooms silently fall back to GHI — acceptable, and
  surfaced via `s_eff_source` in diagnostics.
- living_room+kitchen open space: if its covers ever get labeled, its tag flips ghi→seff and b
  wipes automatically — acceptable, or pin the 8-room open plan to GHI (multi-facade glass may
  fit GHI better than a 2-facade sum)? Decide when labels appear.
- `SEFF_COVER_FLOOR = 0.2` and linear g are owner-spec but uncalibrated — a one-off live test
  (close a cover at steady sun, watch dT/dt step) would pin real transmission for the 3
  verified rooms.
- Should diffuse-only excitation (west-room mornings, S_eff ~110–180) count toward
  planner-grade b identification? Check live `s_hi` histograms before any planner co-enable;
  `MODEL_SOLAR_EXCITATION_MIN` may want a facade-specific look.
- Planner-horizon cover positions: hold-current (v1) vs simulating the shading policy's
  expected closes (sharper afternoon forecast, more coupling) — decide after vN observation.
