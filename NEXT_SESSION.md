# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts + per-feature status) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build
checklist) · [`STORY_SEFF.md`](./STORY_SEFF.md) (the reviewed S_eff spec) ·
[`STORY_SPLIT_TRIO.md`](./STORY_SPLIT_TRIO.md) (the split-AC trio spec) ·
`../hvac-implementation-plan.html` (backbone + prioritized backlog). This file is
the resume pointer + live state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified facts: zone map, EV-FAN-valve cooling chain,
BLOCCO polarity on=block, supervisor architecture, F1/F2/F3/F4 notes). Don't
re-derive verified facts. MASTER_PLAN.md = build checklist. STORY_SEFF.md = the
adversarially-reviewed per-facade solar spec (all 3 slices SHIPPED). STORY_SPLIT_TRIO.md
= the split-AC trio spec (SHIPPED v0.45.0).

STATE (2026-07-10): repo == LIVE == v0.52.0 (1465 tests, ruff clean; DEPLOYED via
HACS + restart 2026-07-10 ~11:51, integration loaded, no ERROR logs, switch.supervisor
still OFF, new opt-in entities present + OFF, cooling_compressor_runtime accumulating).
The 2026-07-10 backlog batch (all opt-in + deploy-dark):
  - v0.46.0  #7  durable presence — watch person.* not the volatile group
  - v0.47.0  P4  Tier-1 feature_graph (sensor.hvac_plan; why did a feature no-op?)
  - v0.48.0  #6  cooling-compressor run-time KPI (sensor, total_increasing)
  - v0.49.0  #2  per-room comfort offset (number.*_offset, per-zone trim)
  - v0.50.0  #3  free-air / windows-open mode (switch.free_air, manual cooling pause)
  - v0.51.0  #5  VMC free-cooling boost (switch.vmc_auto; 2 units, night flush)
  - v0.52.0  #5  VMC night-quiet gate — bedroom unit (VMC 2) silent during Notte
                 while occupied; empty house flushes freely; ground unit never gated
Tier-1 P5 (deviation-space coalescing) DEFERRED: it needs P3 delete-trio first,
which is STOP-gated on a live soak of the merged CoolingController — impossible
while supervisor is OFF. Revisit once supervisor is on and the fold has soaked.
FOLLOW-UPS from this batch: (a) #2 offset is NOT yet folded into
plan_center_schedule (unified-planner base) — add before enabling
switch.unified_planner; (b) VMC thresholds are const defaults — expose in the
options flow if tuning is wanted; (c) #6 `cycles_since_restart` resets on restart
(run-time total is restored).
CRITICAL: the villa still runs on NATIVE KNX — `switch.supervisor` is OFF, so the
WHOLE engine (incl. all six above) is deploy-dark. Every opt-in (seff_enabled,
split_ac, fan_pacing, duty, pv_bias, unified_planner, regime, free_air, vmc_auto)
is OFF. Only read-only diagnostics compute. To light up ANY feature the owner must
first turn ON switch.supervisor. Historical note: on 7/6 the HACS index was stale;
force "Update information" on the Villa HVAC repo in HACS, update, restart HA.
Shipped since the last live-actuating baseline: v0.40.1+v0.41.0 (morning-defect
train), v0.42.0–v0.44.0 (STORY_SEFF), v0.45.0 (split trio), v0.46.0–v0.51.0
(this backlog batch):

0-trio. v0.45.0 — #6 split-AC trio (Cantina wine + Palestra comfort). 3 Daikin
   heads (Zennio KLIC-DD, one shared outdoor PdC) as a synchronized COOL-SIDE-ONLY
   group so it can never create a heat↔cool conflict the KLIC-DD bus can't flag.
   Cantina = self-regulating cool@19 dead-man + RH-aware (dry >65%, relax +1.5
   <55%); Palestra = summer+home+occupied → cool@24 else off; Garage = observe-only.
   `sensor.hvac_split` (live group direction + conflict + per-head, even
   deploy-dark). Pure split_members/split_mode_conflict/split_head_target/
   split_dwell; per-head anti-short-cycle dwell. Fail-safe hands back ONLY managed
   heads. Opt-in `switch.split_ac` (default OFF). NOTE: before enabling, DISABLE
   `automation.circolazione_aria_cantina_vini` first (it fights the controller).

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
0. [DONE 2026-07-09] v0.45.0 deployed + verified inert (HACS + restart ~12:43;
   loaded, no errors; sensor.hvac_split live). v0.44.0 was live 2026-07-08.
   Office/Studio P1 emitter REPAIRED 2026-07-08 (owner-confirmed):
   `switch.office_studio_enabled` = on, zone cooling
   again; its k has NOT re-converged yet (n_k=18, planner_eligible False) — will
   accrue once it runs cooling windows. CAVEAT (2026-07-08): `switch.supervisor`
   is currently OFF → the app is deploy-dark (learning-only, not actuating);
   night-silence + morning wake are running on the LEGACY automations
   (`script.buonanotte_*` + `automation.clima_rientro_in_casa_ripristina_fancoil`,
   triggered by the "Apri Casa" button → `input_select.modalita_casa`), NOT
   villa_hvac #2b — so the v0.41.0 morning fan-ON fix is dormant until supervisor
   is turned on. See the "morning night-silence" note in the auto-memory.
1. v0.45.0 is deployed (0.) — no deploy pending. First watch the read-only
   diagnostics a few days while still dark: S_eff on the model sensors (studio_v
   s_eff peaks mid-afternoon + drops when its cover shades; padronale morning
   s_eff ≈ 0.22·GHI; s_units correct per room) + `sensor.hvac_split` (group
   direction, no phantom conflict, per-head temp/RH). THEN turn ON
   `switch.supervisor` — the master gate for EVERY feature below (S_eff, split
   trio, #2b morning wake); until then the villa runs on native KNX + legacy
   automations and none of the shipped fixes are live. After supervisor is on,
   re-verify the v0.41.0 morning fixes live (padronale morning fan-on + 100%
   sizing; studio_v afternoon shade holds).
1-trio. Split trio: verify `sensor.hvac_split` reads sane (group direction, no
   phantom conflict, per-head temp/RH) while deploy-dark. To enable: DISABLE
   `automation.circolazione_aria_cantina_vini` FIRST (else it fights the Cantina
   controller), then supervisor ON + opt-in `switch.split_ac`. Tune
   `OPT_SPLIT_*` setpoints/RH band/dwell in the options flow. Garage stays
   observe-only until §6 intent is decided with the owner.
2. Geometry validated → owner flips seff_enabled ON (options flow) → watch the
   STORY_SEFF §8 gates ~1 week: (a) G-phase fix (sensor G at 17:30-18:30 >
   13:20 on clear days for office/main_bedroom), (b) b re-identifies (s_hi
   recrosses 150 in S_eff units), (c) capacity_updates flat until (b) then
   resumes (k-freeze visible), (d) no shaded-afternoon comfort loss sub-30°C,
   (e) padronale morning + at-peak behaviors unchanged. Then consider
   DEFAULT_SEFF_ENABLED=True in a follow-up release.
3. THEN backlog: per-room occupancy roster (ep_occ mapped, no consumer;
   Gabriele case), night-guard threshold, legacy cleanup → v1.0.0.
   NEW (owner ask 2026-07-10): **merge free-cooling (#5 auto-coast,
   switch.free_cooling) with the free-air/windows-open concept (#3,
   switch.free_air)** — both mean "the outside air is doing the cooling", one
   automatic, one manual. Design TBD once we understand how the two intertwine
   live (candidates: one "outside air" mode; free-cool conditions + occupied →
   notify "good time to open the windows"; free_air ON auto-implies free-cool
   logic; shared threshold/gating). Deliberately NOT designed yet — gather a
   few weeks of live behavior with both switches first.
   NEW (owner ask 2026-07-10, evening): **free_air evolves into per-room "Open
   windows"** — (a) rename switch.free_air → "Open windows"; (b) make it
   PER-ROOM (one switch per cooled zone, pausing just that zone — today it's
   house-wide); (c) attach each room to a physical WINDOW CONTACT SENSOR once
   those get INSTALLED on the cooled rooms (today only the 3 vasistas on
   radiant rooms exist — the known #4 gap). The #4 WindowController already
   handles contact→pause debounce/restore, so a fitted sensor slots into the
   existing `window` key per zone; the manual per-room switch is the fallback
   for rooms still without a sensor (and the override). Sequence/design
   TOGETHER with the free-cool merge above — same "outside air" concept.
   DONE from the old list: free-air (v0.50.0), presenza_adulti (v0.46.0), VMC
   boost (v0.51/52), per-room offsets (v0.49.0), KPI energia (v0.48.0).

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
