# STORY F4c — Unified forecast planner (build plan)

**Status (2026-07-02):** Phases **0–6 DONE** (v0.32.0 → v0.38.0), CI green.
Phases 0–5 shipped + DEPLOYED live at v0.37.0 (plan-only). **Phase 6 BUILT
deploy-dark** (v0.38.0): `switch.unified_planner` (default OFF) lets the forecast
schedule DRIVE the band center for planner-ELIGIBLE rooms via the pure `planner_ref`
gate (clamped into [comfort_floor, duty_comfort_max]; false-safes to the reactive
ladder on switch-off / ineligible / stale / deep-setback / missing-point). The
schedule is a SLOW-moving cached reference (recomputed at the forecast cadence or on
a mode change). **ACTIVATION (flipping the switch) is still gated** on mild-weather
validation data + per-room k-convergence — and is INERT today anyway (no room is
planner-eligible yet: solar_excitation 0, k unconverged). **Phase 7** = validate
live over a mild season, then retire the dual-path ladder. **Phase 8** stays a
NON-GOAL. Shipped so far: Phase 0 input hardening (B4+C5),
Phase 1 composition contract + `OPT_COMFORT_FLOOR` + `compose_center` + C1
NightSilenceController, Phase 2 `supervisor/` pure package split (C2), Phase 3
`SupervisorConfig` (C3), Phase 4 identifiability gating + `planner_eligible` (D1),
Phase 5 `plan_center_schedule` → `CenterSchedule` on `sensor.hvac_plan.center_schedule`
(PLAN-ONLY, drives nothing). 309 tests.

---

**Original plan follows** (authored 2026-07-02 after a design judge-panel:
3 positions → adversarial critique → synthesis). Owner decided to build the full
thing (Track A + Track B together) and fold in the remaining §B/§C/§D engine-hardening.
This doc is the executable plan for a **fresh build session** — read it top to bottom
plus `CLAUDE.md`, `ENGINE_REVIEW.md`, `MASTER_PLAN.md`, and the panel synthesis captured
in that session's transcript.

Live baseline at authoring time: **v0.31.0**, LIVE == repo, opt-ins
supervisor+fan_pacing+duty_cycle+solar ON; pv_bias/comfort/regime/proportional-shading OFF.

---

## 0. What this is (and the one boundary that matters)

**Goal.** Replace the myopic, fixed-priority *composition* of the fancoil band `center`
(today: `#8 mode-override → PV bank/coast → #9 pre-cool offset → comfort_relax`, each
looking only at *now*) with ONE **unified 12 h forecast planner** that jointly schedules a
per-room band-center **reference trajectory** — reasoning together about the outdoor+solar
forecast, the forecast peak, PV/consumption balance, return-home arrival ETA, and duty
run/rest coalescing. This is the "F4c MPC / unified forecast" the owner asked for.

**The pre-cool IS in scope** — #9/#7 forecast pre-cool (`precool`, `precool_offset`,
`schedule_precool`, `build_room_plans`) is one of the four feed-forward inputs the planner
subsumes. So are the PV bias floor, the #8 arrival ramp, and the duty coalescing windows.

**THE BOUNDARY (non-negotiable, from the synthesis + adversarial critique).** The planner
emits a **reference only** — a per-zone center trajectory + a house BLOCCO/duty *intent*.
It **NEVER** writes a lever, never calls a service, never makes the closed-loop RUN/REST or
comfort decision. The existing **reactive layer keeps the comfort guarantee**:
`band_step`'s wide hysteresis + `duty_comfort_max` ceiling + comfort-breach-forces-RUN,
all **model-free**. The reactive layer *clamps* the reference into `[comfort_floor,
duty_comfort_max]` before acting on it. This is a **hierarchical / reference-governor MPC**,
which is legitimately "MPC" — but it is **NOT** the fully-closed-loop optimizer where comfort
lives inside the cost function.

> **Why the boundary holds even though the owner wants "the full thing":** the 4-param
> thermal model provably cannot reproduce the measured ~0-net cooling at the ~34 °C peak, and
> per-room `k` is not converged. An optimizer that owns comfort would bet comfort on
> predictions that are wrong exactly where cooling is the binding constraint. Keeping comfort
> reactive means **model error costs efficiency, never comfort** — the property this occupied
> house depends on. Phase 8 below is the *optional, separately-gated* path to cross this line
> if the owner later insists; it is a conscious decision with its risks spelled out, and is a
> **non-goal** for this build.

---

## 1. Invariants that must hold at EVERY phase (regression = stop)

1. **Comfort is guaranteed by the reactive band, not the model.** `band_step` hysteresis +
   `duty_comfort_max` ceiling + comfort-breach-forces-RUN survive untouched. Property to
   test: for any reference center in `[floor, ceiling]`, a room never sits above
   `center + B/2` for more than one cycle.
2. **Fail-safe is sacred.** On unload / `EVENT_HOMEASSISTANT_STOP` / master-off / crash:
   release BLOCCO (fail-open), fans → AUTO, presets → `auto` (B1), epoch guard against
   post-hand-back re-slam. The planner owns **no lever** and **no BLOCCO write** — so it adds
   nothing to hand back.
3. **Deploy-dark + per-feature opt-in.** Nothing new actuates until its switch is on. The
   planner ships **plan-only first** (drives nothing), then behind `switch.unified_planner`.
4. **Pure cores stay pure** (no `homeassistant` imports) and unit-tested. Every phase lands
   code **plus** tests, ruff clean, CI green (HACS + hassfest + ruff/pytest on HA 2026.4.3 /
   Py 3.14). Small commit + tag + gh release per increment.
5. **A comfort FLOOR is now first-class** (new — see Phase 1). Today only the ceiling
   (`duty_comfort_max`) is enforced; nothing bounds *over*-pre-cool (center driven too low →
   cold occupied rooms + wasted energy). The planner can drive the center DOWN, so the floor
   must exist before any reference drives control.
6. **Hard (gain-limited) rooms stay ADVISORY** until their `k` converges — reference clamped
   to `max_lead` / `max_depth`, same gate style as `REGIME_K_CONF_MIN`.

---

## 2. Should §B/§C/§D fold in? YES — most are prerequisites, not add-ons

| Item | Fold-in? | Why it belongs in THIS build |
|---|---|---|
| **C2** split `supervisor.py` → `arbiter/thermal/planner/control_law/returnhome` | **Prerequisite** | The `planner` package is literally where `plan_center_schedule()` lives. Do the split first so the new code has a clean home + the pure cores it reuses are already grouped. |
| **C3** `SupervisorConfig` parsed-once dataclass | **Prerequisite** | The planner adds many options; parse+clamp them once instead of scattering more `float(entry.options.get(...))`. Also splits `HouseState` into measured-state vs config — the planner needs the measured half clean. |
| **C4** explicit opt-in dependency graph | **Co-requisite** | The planner adds `switch.unified_planner` with real deps (needs fan_pacing + comfort_floor + per-room k) and **retires** the "don't co-enable pv_bias + regime" caveat (one scheduler owns the center). C4 *is* the resolution of that caveat. |
| **D1** thermal identifiability gating | **Safety gate** | The planner's schedule is only trustworthy when the model is identified. D1 (trust `abc` only when solar range was excited; `k` as night-calibrated lower bound; hard-room advisory) is the gate that decides *when the reference may drive control*. |
| **C1** NightController → arbiter controller | **IN → Phase 1** (owner) | #2b camere-silenziose is the last direct writer bypassing the arbiter. The Phase-1 composition contract is *incomplete* while a second writer exists. Fold it onto the arbiter so ALL center/lever writes go through one place. |
| **B4** state-robustness leftovers | **Fold-in (input trust)** | consenso `unavailable` → freeze duty timer / skip learning window; sort `_cloud` before `_forecast_cloud_at` early-break; legacy `"closed"` vs numeric cover position; stale-fused-temp diagnostic. These harden the *inputs* the planner keys off. Do before trusting the planner. |
| **C5** perf/robustness nits | **Fold-in (cheap)** | cache `shadeable_covers` (full registry scan every 30 s) + registry-updated listener; wrap each lever `_call` in `asyncio.wait_for` (one wedged KNX write can't stall a cycle — matters more once the planner adds work); hoist the `astral` import. Slot into Phase 0. |
| knx.yaml salotto duplicate-ID | **Fold-in (live wart)** | Not repo code — the KNX config defines the salotto fancoil twice (`5/2/1`, `5/4/1`). Clean it so #3's salotto entities are unambiguous. Do opportunistically. |

Net: the build is **hardening-first, then scaffolding, then planner** — the refactors (C2/C3/C4)
are the planner's skeleton and D1 is its safety gate, so they're not a detour.

---

## 3. The phased build plan

Each phase = one or more small PRs, own commit+tag+release, CI green, deploy-dark or opt-in.
Dependencies are strict (a phase lists what must be DONE first). Ordered so the system is
shippable and safe after every phase.

### Phase 0 — Input hardening + live wart  *(folds B4, C5, knx wart)*
- **B4**: consenso `unavailable` → freeze the duty stint timer + skip the F2 learning window
  (do not treat as "not cooling"); sort `self._cloud` before the `_forecast_cloud_at`
  early-break; handle legacy `"closed"` vs numeric `current_position` in the cover lever;
  add a diagnostic (WARNING + sensor attr) when a leader's fused temp is `None` > N cycles.
- **C5**: cache `shadeable_covers` behind a registry-updated listener (stop the 30 s full
  scan); wrap each lever service call in `asyncio.wait_for(..., LEVER_CALL_TIMEOUT)` so a
  wedged KNX write can't stall the cycle (log + move on); hoist `astral` import to module top.
- **knx.yaml**: remove the duplicate salotto fancoil GA definitions (`target_temperature_address`
  dup lines ~249-250; the `5/2/1` / `5/4/1` unique-ID collisions). Live-config change → do via
  the KNX yaml + reload, verify no dup-ID ERROR at boot.
- Deliverable: hardened inputs; no behavior change to actuation. Ship deploy-dark-safe.

### Phase 1 — Composition contract + comfort floor + observability + unify writers  *(Track A, foundation)*
- **Named composition order.** Extract the implicit `_cycle` order into a documented
  `COMPOSITION_ORDER` table/constant + a module docstring invariant: *"no feature may raise a
  center above `duty_comfort_max`; only `disabled`/`window`/`free_cool` policies may drive
  `building_protection`."* No behavior change.
- **Comfort FLOOR invariant (NEW).** Add `OPT_COMFORT_FLOOR` (or derive from house_setpoint −
  a max-precool-depth) enforced symmetrically to `duty_comfort_max`: no center-lowering feature
  (PV bank, #9 pre-cool) can push a center below it. First-class, tested.
- **Cross-feature invariant tests** (`tests/test_composition.py`): co-enable #8 + PV + precool +
  band → assert final center ∈ `[comfort_floor, duty_comfort_max]`; assert only
  disabled/window/free-cool emit `building_protection`; assert pv_bias and regime cannot both
  drive the center (encode the engine.py ~979 comment as a real assertion).
- **Characterization tests** pinning the CURRENT ladder center for a matrix of
  `(mode, pv_mode, precool, comfort_relax)` — REQUIRED so Phase 6's replacement is provably
  behavior-preserving when the planner is off.
- **Observability**: a diagnostic sensor (extend the `hvac_levers` idea) showing each feature's
  contribution to the final center per cycle.
- **C1 — NightController → arbiter (fold in, owner: IN).** Convert #2b camere-silenziose from the
  last *direct* writer into a `night_silence` controller emitting `{switch:manuale, fan:pct}`
  lever opinions merged like the others, so ALL writes flow through the one arbiter and the
  composition contract above is actually complete. Then simplify `_startup_resync` (drop its
  `apply_house_mode` night branch). Tests: #2b still fires the Padronale/Gabriele night burst +
  heat-guard, and a reboot-in-Notte re-enters silence via the controller (not the resync).
- Deliverable: the real defect (myopic, comment-only composition) fixed + regression-proofed;
  the comfort floor closed; every writer through the arbiter. **Valuable even if Track B never ships.**

### Phase 2 — Split `supervisor.py`  *(C2, scaffolding)*
- Pure package: `arbiter` (LeverState/reconcile/merge/lever-keys), `thermal` (RLS/blend/estimator),
  `planner` (RunPlan/simulate_room/schedule_precool/solar_curve), `control_law`
  (band_step/capacity_fan/duty_decision/coalesce_phase), `returnhome` pure core. Keep the
  no-HA-imports property; keep public import paths working (re-export from `supervisor` or
  update importers). All 262+ tests green unchanged.
- Deliverable: the `planner` module exists as the home for `plan_center_schedule()`.

### Phase 3 — `SupervisorConfig` + measured/config split  *(C3)*
- One `SupervisorConfig.from_entry(entry)` that coerces+clamps every option once/cycle
  (incl. the new planner + comfort-floor options). Split `HouseState` into measured-state +
  config so the planner takes a clean measured snapshot. Kills the ~33 scattered try/excepts.
- Deliverable: clean config surface for the planner's many knobs.

### Phase 4 — Identifiability gating  *(D1, safety gate)*
- Raise `abc` confidence only when the fed windows spanned a real solar range (don't trust a
  `b` never excited); treat hard-room `k` as a night-calibrated lower bound; expose a per-room
  "planner-eligible" flag = (k converged AND abc identified). Document that hard rooms stay
  advisory. No actuation change (gates a later phase).
- Deliverable: a trustworthy per-room signal for "may the reference drive this room's center?"

### Phase 5 — `plan_center_schedule()` PLAN-ONLY  *(Track B core, drives nothing)*
- Pure `plan_center_schedule(measured, config, forecast, solar_curve, eta, pv_inputs) →
  CenterSchedule` in the new `planner` module. Composes the ALREADY-SHIPPING pure cores:
  `schedule_precool` (pre-cool depth/start), `energy_precool_decision` (PV bank/coast floor),
  `return_lead_time` (#8 arrival readiness), `coalesce_phase`/`run_rest_durations` (duty
  run/rest intent) → ONE per-leader-zone hourly center trajectory + a house BLOCCO/duty intent.
- `CenterSchedule` dataclass: per-zone hourly `center_ref` points + `.at(zone, now)` lookup +
  a validity/staleness stamp. Unit-tested in isolation like every other pure core.
- Surface on `sensor.hvac_plan` as the reference schedule — **PLAN-ONLY, deploy-dark, driving
  nothing**. This is the "unified forecast" made visible; it closes the plan-vs-actual *display*
  gap without touching a lever. Watch it against live forecasts (esp. the mild season the
  project has NEVER measured — Stage 2 was a heatwave).
- Deliverable: the joint scheduler exists and is observable, at zero control risk.

### Phase 6 — Reference DRIVES the center  *(Track B activation, behind the switch)*
Preconditions: Phases 1–5 done; **mild-weather validation data exists**; comfort floor enforced;
characterization tests pin the ladder; per-room k converged for the target rooms.
- **`switch.unified_planner`** (deploy-dark, default off) via the C4 dependency graph (needs
  fan_pacing; surfaces WHY it's inert if a dep is missing).
- In `FanBandController`: `center = clamp(state.center_ref.at(zone, now), comfort_floor,
  duty_comfort_max)` **when** the schedule is present + switch on + zone planner-eligible; ELSE
  the existing ladder (fallback path preserved). `band_step`, `capacity_fan`, `reconcile`,
  BLOCCO, fail-safe all UNCHANGED.
- **#8 / planner precedence (resolve explicitly — critic flagged double-count):** #8 keeps its
  effective-mode override as the GATE — `Vacanza` while truly-away-waiting (planner emits no
  active-cooling reference; deep setback), `Casa` once in the arrival window. The planner then
  SHAPES the center trajectory *within* the Casa window (subsuming #8's ramp/lead-time as a
  "be at comfort by ETA" constraint). Rule: `AwayReturnController` owns the mode gate; the
  planner owns the ramp shape. No zone gets a center from two sources.
- **Stale-schedule handling:** if the schedule is stale/failed refresh → fall back to the base
  center (`house_setpoint + mode_offset`), NEVER a stale 12 h reference (which can be
  *confidently wrong* — sized to a peak that moved). Do NOT re-validate the schedule against the
  live forecast inside the fast 30 s loop (keep the reactive loop model-free); staleness = age
  of the last good refresh only.
- **Roll out EASY rooms first** (living_room/office — more identifiable). Hard gain-limited rooms
  stay advisory-clamped; do not block the whole feature on their k (may never converge at peak).
- Deliverable: the planner drives control, safely, reversibly (flip the switch), easy-rooms-first.

### Phase 7 — Validate + retire  *(C4 closure)*
- Validate live over a mild season. Then retire the ladder branches + the pv/regime co-enable
  caveat as dead paths (the planner owns the center). Keep the fallback only for
  planner-off / stale.
- Deliverable: one composition path; the "N separate features" framing is gone — replaced by a
  single scheduler feeding one reactive layer.

### Phase 8 — TRUE F4c (OPTIONAL, GATED, DEFAULT NON-GOAL)
- Only if the owner consciously chooses to cross the boundary: let the planner make the
  closed-loop RUN/REST / comfort decision from predictions (comfort inside the cost function).
- **Do NOT build by default.** Requires: converged k on the target rooms, a full season of
  Phase-5/6 validation showing the schedule beats the reactive band, and an explicit owner
  decision accepting that comfort now depends on the model. Almost certainly never for the
  hard rooms. Keep the reactive band as a safety floor even here (so it's still not *pure* F4c).

---

## 4. Key design decisions to resolve during the build (with recommendations)

1. **#8 vs planner precedence** — RESOLVE as in Phase 6: #8 = mode gate, planner = ramp shape.
   Add a test asserting a single center source per zone.
2. **Comfort floor source** — DECIDED (owner, §7): explicit `OPT_COMFORT_FLOOR`, default
   house_setpoint − 2 °C, options-flow tunable, clamped to a sane absolute range.
3. **Planner cadence** — RECOMMEND the forecast-refresh cadence (30 min), NOT every 30 s. The
   reactive loop stays 30 s; the reference is a slow-moving schedule.
4. **Eligibility gating granularity** — per-room planner-eligible flag (Phase 4), not a global
   switch, so easy rooms benefit while hard rooms stay advisory.
5. **Fallback dual-path lifetime** — the ladder stays as fallback through Phase 6; retire in
   Phase 7 only after validation. Characterization tests keep the two reconciled meanwhile.

---

## 5. Risks carried forward (from the adversarial critique — mitigations baked into the phases)

- **Reward mismatch to data:** the joint-scheduling upside is real only on gain-limited rooms at
  peak (where the model is fiction) or in mild weather (no data yet). → Phase 5 plan-only-first
  gathers the mild-season data BEFORE any lever moves; hard rooms stay advisory.
- **Scope-creep to F4c:** a joint scheduler is one commit from the deferred MPC. → The boundary
  (§0) is an enforced invariant + Phase 8 is explicitly opt-in/gated. Hold the line in review.
- **Dual center-composition path** must stay reconciled. → Characterization tests (Phase 1) +
  retire only post-validation (Phase 7).
- **Stale 12 h reference = confidently wrong**, not cleanly absent. → Fall back to base center on
  stale; no model coupling in the fast loop (Phase 6).
- **Over-pre-cool (center too low) = cold rooms + wasted energy**, unprotected today. → Comfort
  floor (Phase 1) is a prerequisite for Phase 6.
- **Opportunity cost:** this is a multi-phase build for a still-unmeasured regime. → Phases 0–1
  are high-value regardless; Track B activation (6–7) is gated on data actually justifying it.

---

## 6. How the next session should start

1. Read: this doc, `CLAUDE.md`, `ENGINE_REVIEW.md` §B/§C/§D, `MASTER_PLAN.md` engine-hardening
   table, `custom_components/villa_hvac/{engine.py,policies.py,supervisor.py,returnhome.py}`, and
   `STORY_PV_BIAS.md` (PV inputs the planner reuses).
2. Confirm live baseline (`sensor.hvac_levers` = 0 held, BLOCCO off, v0.31.0) before touching code.
3. Execute **Phase 0 → 1 → 2 → 3 → 4 → 5** as separate small PRs (each: code+tests, ruff, CI green,
   commit+tag+gh release, deploy-dark-safe). **Stop before Phase 6** and check in with the owner —
   Phase 6 needs mild-weather validation data + a k-convergence check + owner sign-off.
4. Keep every phase behind deploy-dark / opt-in; the reactive comfort layer and fail-safe are
   never in the diff except to *preserve* them.
5. ASK before any live HVAC write; deploy each release via HACS + restart only with owner OK.

## 7. Owner decisions (RESOLVED 2026-07-02 — build to these)

1. **Comfort floor** — DECIDED: explicit `OPT_COMFORT_FLOOR`, default **house_setpoint − 2 °C**
   (options-flow tunable, clamped to a sane absolute range). Enforced symmetrically to
   `duty_comfort_max` in Phase 1; prerequisite for Phase 6.
2. **C1 (NightController → arbiter)** — DECIDED: **IN.** Fold it into Phase 1 so ALL center/lever
   writes go through the one arbiter and the composition contract is complete (no bypassing
   direct writer). Accept the mild camere-silenziose behavior touch; cover it with tests +
   a startup-resync simplification, and validate #2b (Padronale/Gabriele night burst) still fires.
3. **Phase 8 (true comfort-in-optimizer F4c)** — DECIDED: stays a **NON-GOAL / owner-gated.** The
   planner emits a reference only; the reactive band keeps the model-free comfort guarantee. Do
   not build Phase 8 in this program.
