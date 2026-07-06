# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts + per-feature status) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build
checklist) · [`STORY_SEFF.md`](./STORY_SEFF.md) (the reviewed S_eff spec) ·
`../hvac-implementation-plan.html` (backbone + prioritized backlog). This file is
the resume pointer + live state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified facts: zone map, EV-FAN-valve cooling chain,
BLOCCO polarity on=block, supervisor architecture, F1/F2/F3/F4 notes). Don't
re-derive verified facts. MASTER_PLAN.md = build checklist. STORY_SEFF.md = the
adversarially-reviewed per-facade solar spec (all 3 slices SHIPPED).

STATE (2026-07-06): repo = v0.44.0 (1399 tests, ruff clean); LIVE = v0.40.0 —
the owner has NOT yet deployed (HACS index was stale on 7/6; force "Update
information" on the Villa HVAC repo in HACS, update, restart HA). Between live
and repo: v0.40.1+v0.41.0 (morning-defect train: fan lever ON/OFF, night wake
clock-derived, RUN sizing law run_fan_pct, shading never-raise + SW band) and
v0.42.0–v0.44.0 (STORY_SEFF, this session):

1. v0.42.0 — S_eff pure law (supervisor/solar.py: vertical-glazing beam tilt
   rb clamped 3.0 per-facade AND on the zone beam sum ≤2.69×GHI; diffuse floor
   0.22·g; cover transmission 0.2 floor, None=open+degraded; per-facade mean g,
   cover-multiset units tag "seff1:225x1,292x1") + inert engine feed +
   s_eff/s_eff_source/s_units diagnostics on the model sensors.
2. v0.43.0 — units seam: ThermalEstimator.ensure_units runs EVERY cycle at the
   engine consumption seam (independent of model_learning_enabled — 2 CRITICAL
   spec-review findings), rebase_solar_units wipes b→prior + reopens its
   covariance + zeros s_hi + drops the buffer; k-FREEZE (capacity updates only
   on an identified {a,b,c} — post-rebase auto-suspends); MODEL_GAP_MAX_S=180s
   buffer gap guard; estimator ingests z.s_eff source-gated (facade/ghi learn,
   fallback/facade_degraded skip); consumers switched: band trio+fold,
   house_load_index, plan g_sum, ReturnRoom.s_eff.
3. v0.44.0 — planner horizon (astral az+el track → engine._zone_solar_curves;
   flat mode propagates the LIVE s_eff/GHI ratio, never silently the house
   curve; solar_domain markers on PlanView/CenterSchedule/hvac_plan), per-zone
   PV effectiveness (_house_cooling_model deleted), sensor G on the
   control-facing S_eff, LIGHT-UP: SEFF_CONSUMERS_READY=True + the seff_enabled
   options toggle (default OFF, opt-in).

S_eff verified facts baked in: label south = real SW facade 225°, west = WNW
292° (only these two mapped; east/north excluded — non-derivable); rooms with
apertures: main_bedroom (west+south), office (west), studio_v (south); all
other leaders = GHI identity (no wipe, byte-identical). Flipping seff_enabled
either way rebases affected rooms' b to prior (re-learns in ~1-2 weeks; k
frozen meanwhile; the 5 currently planner_eligible rooms TEMPORARILY de-el
igible until s_hi re-excites in S_eff units — fine, unified_planner is OFF).

NEXT STEPS (in order):
1. Owner deploys v0.44.0 (HACS force-refresh + restart) → live re-verify the
   v0.41.0 morning fixes (padronale morning fan-on + 100% sizing; studio_v
   afternoon shade holds) AND watch the S_eff diagnostics a few days (model
   sensor attrs: studio_v s_eff peaks mid-afternoon + drops when its cover
   shades; padronale morning s_eff ≈ 0.22·GHI; s_units correct per room).
2. Geometry validated → owner flips seff_enabled ON (options flow) → watch the
   STORY_SEFF §8 gates ~1 week: (a) G-phase fix (sensor G at 17:30-18:30 >
   13:20 on clear days for office/main_bedroom), (b) b re-identifies (s_hi
   recrosses 150 in S_eff units), (c) capacity_updates flat until (b) then
   resumes (k-freeze visible), (d) no shaded-afternoon comfort loss sub-30°C,
   (e) padronale morning + at-peak behaviors unchanged. Then consider
   DEFAULT_SEFF_ENABLED=True in a follow-up release.
3. THEN backlog: per-room occupancy roster (ep_occ mapped, no consumer;
   Gabriele case), "free-air/windows-open" mode (Via does NOT stop cooling),
   night-guard threshold, fix group.presenza_adulti, VMC boost, per-room
   setpoints, KPI energia, legacy cleanup → v1.0.0.

TIER-1 TRAIN (STOP-gated on the live soak of the deployed train): P3
delete-trio (the trio still carries all law changes verbatim; sequence AFTER
the soak) → P4 feature_graph → P5 R2 deviation-space → P6 R3 quorum.
unified_planner + regime enabling stays gated (mild weather + k-convergence;
NOTE 5 rooms were planner_eligible live pre-S_eff — after the b wipe they
re-gate, expect eligibility back within ~2 weeks of flag-on).

RULES unchanged: strict deploy-dark for anything new actuating; small commit +
tag + gh release per increment; pre-tag adversarial review (workflow if budget
allows, else inline 3-lens + refutation); pytest + ruff green on the pinned
target (pytest-homeassistant-custom-component==0.13.324 = HA 2026.4.3 /
Py 3.14); fail-safe invariants; HA connector read-only for diagnosis; owner
deploys via HACS update + restart. Known quirks: ~40s KNX unavailable blips
(nightly ~03:00 backup) — ignore singles, ThermalEstimator gap guard now
handles the long ones.
```
