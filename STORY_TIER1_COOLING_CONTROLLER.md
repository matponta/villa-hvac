# STORY TIER-1 — One `CoolingController` (structural merge + composition fixes)

**Status (2026-07-02): LOCKED DESIGN — not started.** Authored after a 3-design judge
panel (fix-then-merge / merge-early / pure-core-shell) → 3 adversarial verdicts
(behavior-preservation, safety, cost-benefit) → this synthesis. Baseline at authoring:
repo **v0.38.0** == **LIVE v0.38.0** (deployed 2026-07-02), 322 tests green.
Live opt-ins: `supervisor` + `fan_pacing` + `duty_cycle` + `solar` ON;
`regime` / `pv_bias` / `comfort` / `unified_planner` OFF. **Every phase below ships
deploy-dark and flips no switch.**

This doc is the executable plan for a **fresh build session** — read it top to bottom
plus `CLAUDE.md`, `MASTER_PLAN.md`, `ENGINE_REVIEW.md`, and
`STORY_F4C_UNIFIED_PLANNER.md` (style + boundary precedent). Line numbers are
verified against v0.38.0; re-verify before editing.

---

## 0. What this is (and the one boundary that matters)

**Goal.** Fold the three stateful cooling controllers —
`FanBandController` (policies.py:423, band slam + capacity fan + manuale),
`DutyController` (policies.py:363, stint/cooloff BLOCCO), and
`RegimeCoordinator` (policies.py:743, MEDIUM-regime RUN/REST sync) — into **ONE
`CoolingController`**, so the load-bearing composition that today lives as *engine
list-ordering* (regime-BLOCCO prepended before duty, engine.py:766-768; controllers
merged before pure policies, engine.py:778; the `phase_override` handoff,
engine.py:765→policies.py:515-517) becomes **explicit, internal, and
unit-testable**. On the way in, fix the three known composition defects:

1. **R1 — one resolved center.** Today `engine._regime_step` hands `coalesce_phase`
   the **BASE** center (engine.py:985-988) while `FanBandController` slams the
   planner/PV/relax-**SHIFTED** one (policies.py:489-511). One pure
   `resolve_center` writes ONE per-leader field everybody reads.
2. **R2 — deviation-space coalescing** with the ANCHOR/BANK REST partition, the
   crossing-center cap, and deletion of the `comfort_relax` double-count
   (policies.py:780 subtracts it from temp while compose_center already added it
   to the center, control_law.py:229-230).
3. **R3 — REST-quorum guard** so a structurally gain-limited room (the verified
   ~0-net padronale at the 34 °C peak) can never deadlock the compressor in
   permanent RUN.

Plus **R4 — `feature_graph`**: `{enabled, active, inert_reason}` per optimizer on
`sensor.hvac_plan`, so live validation can see *why* a feature did nothing.

**THE BOUNDARY (non-negotiable).** This is a **STRUCTURAL** tier: byte-identical
behavior at the KNX-lever level everywhere R2/R3 don't *deliberately* change it —
and R2/R3 only change the `OPT_REGIME_ENABLED` path, which is default-OFF and has
**zero live runtime history**. Comfort stays **model-free**: `band_step` hysteresis
+ the `duty_comfort_max` ceiling + comfort-breach-forces-RUN are untouched on every
path. This is **NOT** F4c Phase 8 (comfort-in-optimizer stays a NON-GOAL), it
enables **no** switch, and it does **not** redesign the fail-safe — it preserves it
byte-for-byte and adds exactly one pinned hardening (the per-lever epoch check).

---

## 1. The architecture

### 1.1 Foundation — `resolve_center` (R1, lands first)

- **`CenterResolution`** (frozen: `center, base, source, floored, planner_driven`)
  and pure **`resolve_center(zone, state) -> CenterResolution`** in
  `supervisor/planner.py` (planner already imports control_law, so calling
  `compose_center` there is acyclic). Precedence: **`planner_ref` ▸
  `compose_center` ladder ▸ base**, ONE clamp site into
  `[comfort_floor, duty_comfort_max]` — a verbatim relocation of the wiring
  currently inline at policies.py:489-511.
- Pure **`annotate_centers(state) -> HouseState`**: for every cooling **leader**
  passing the eligibility mirror of policies.py:479-482 (leader, enabled, not
  paused, not free-cool, not bedroom+night, base not None),
  `dataclasses.replace()` the `ZoneSnapshot` with `resolved_center`,
  `center_source`, `center_floored`, `planner_driven` (new fields on
  `supervisor/model.py`'s ZoneSnapshot, default `None/"none"/False/False`).
- **Engine ordering (load-bearing, comment it in `_cycle`):** the annotate call
  goes immediately after `state = self._maybe_refresh_schedule(state)`
  (engine.py:755) — i.e. after the away-return mode override (~engine.py:745,
  which rewrites `house_mode`/`mode_offset` and therefore the base center), after
  `_pv_bias_apply` (engine.py:751, attaches `pv_mode`/`pv_floor`), and after the
  schedule attach. Wrong slot ⇒ resolution silently loses PV/planner/precool.
- **Readers:** `FanBandController.__call__`'s inline center block
  (policies.py:489-511) collapses to *read `z.resolved_center`*; the engine's
  duplicated `_center_compositions` (engine.py:911-953) is rewritten to read the
  annotated fields (deleting the second copy of the eligibility+compose logic —
  the actual hazard class). **`_regime_step`'s scalar base center
  (engine.py:985-988) is deliberately NOT touched in R1** — that changes in R2;
  R1 is a pure identity refactor.
- **Loud fallback:** an eligible cooling leader with `resolved_center is None` on
  an actuating cycle is a WARN-once condition (pattern of `_track_stale_temp`,
  engine.py:660-680) and a test failure — never a silent degrade to `center_base`
  (which would erase live pre-cool without failing anything).

### 1.2 The final `CoolingController` shape (lands in P2)

One class in `custom_components/villa_hvac/policies.py`, replacing the trio.
Bodies are **moved verbatim** — in-place dict mutation semantics preserved, no
frozen-state rewrite, no module relocations.

```python
class CoolingController:
    """The one cooling organism: regime coalescing + band slam/fan + duty BLOCCO,
    folded. Called ONLY on actuating passes (timers advance there and nowhere
    else); the #11 plan view reads .duty / .regime_state read-only.

    BLOCCO precedence (was engine list-order, engine.py:766-768) is the ONE
    explicit line in __call__: a coalescing RELEASE beats the duty opinion.
    duty_pass ALWAYS runs — its timers must advance even while regime overrides
    (identical to today, where DutyController is called every actuating cycle
    and merely loses the merge)."""

    def __init__(self) -> None:                       # no-arg ctor (test pin)
        self._states: dict[str, BandState] = {}       # verbatim policies.py:440-442
        self._last_fan: dict[str, int] = {}
        self._duty = DutyState()                       # NAME KEPT: tests poke c._duty
        self._rs = RegimeState()                       # verbatim policies.py:755-756

    @property
    def duty(self) -> DutyState: ...                   # was policies.py:374-377 (plan view)
    @property
    def regime_state(self) -> RegimeState: ...         # was policies.py:758-760 (diagnostic)

    # ---- public sub-passes: bodies MOVED VERBATIM; unit tests port as renames ----
    def regime_pass(self, state) -> tuple[dict[str, str], str | None]:
        # = engine._regime_step (engine.py:955-993) + RegimeCoordinator.step
        #   (policies.py:762-791) folded, INCLUDING the not-coalescing branch
        #   (engine.py:965-966): gate-off is a RESET pass (regime="low" flows
        #   through and clears _rs) — NEVER a skip. Gating reads move from the
        #   live hass helpers (engine.py:962-963) to state.duty_enabled /
        #   state.fan_pacing_enabled — a DOCUMENTED one-cycle snapshot-
        #   consistency deviation (see §4), not asserted-away as "identical".
    def duty_pass(self, state) -> Desired:
        # = DutyController.__call__ verbatim (policies.py:379-420): explicit
        #   {BLOCCO_LEVER: BLOCCO_RELEASE} on disable (:385-391), B4 transient
        #   consenso FREEZE (:397-408), duty_decision advance (:410-419).
    def band_pass(self, state, phase_override=None) -> Desired:
        # = FanBandController.__call__ verbatim (policies.py:460-554) with the
        #   R1 simplification: center = z.resolved_center (else center_base).
        #   _release_all (:444-458), night-bedroom pop (:476-478), released-zone
        #   BandState bookkeeping (:527-532), _last_fan hysteresis (:544-550):
        #   ALL byte-preserved, including the asymmetries (see §4).

    def __call__(self, state: HouseState) -> Desired:  # the merge-controller entry
        override, regime_blocco = self.regime_pass(state)
        duty_out = self.duty_pass(state)               # ALWAYS — timers advance
        out: Desired = dict(self.band_pass(state, phase_override=override))
        assert BLOCCO_LEVER in duty_out                # loud, never a prod KeyError
        out[BLOCCO_LEVER] = (
            regime_blocco if regime_blocco is not None
            else duty_out.get(BLOCCO_LEVER)
        )
        return out
```

- **Internal order = today's engine order:** regime first (engine.py:765), duty
  then band (controllers tuple order, __init__.py:66). Sections are lever-disjoint
  and don't cross-read mutated state, but the order is pinned anyway (post-state
  asserted per step in the identity harness) because R2/R3 add cross-section data
  flow.
- **Regime classification placement:** inside `regime_pass` — ONE consumer-side
  computation (house_load_index + at_peak + free_cool + select_regime, lifted
  verbatim from engine.py:967-984). The plan view's read-only duplicate
  (engine.py:826-835) stays **byte-untouched until P6**, then unifies behind an
  equality pin. Both `regime_classified` (always-on, plan-view semantics) and
  `regime_driving` ("low" unless regime∧duty∧fan_pacing, engine.py:960-966) are
  surfaced in diagnostics so they can never be conflated by position.
- **Engine after the fold:** the actuate block (engine.py:760-778) collapses to
  `ctrl_outputs = [c(state) for c in self.controllers]` +
  `merge_desired([*ctrl_outputs, *pure_outputs])`, keeping the controllers-first
  comment. Deleted: `_regime_step` (955-993), `self.regime` (551), the
  `_fan_controller` extraction (555-557). The `_duty_controller` extraction
  (541-543) becomes `self._cooling`; the plan-view duty read (816-818) becomes
  `self._cooling.duty if self._cooling else DutyState()`. The reconcile loop
  (779-789), lock/epoch machinery (718-729), `thermal.observe` every cycle
  (740), `async_fail_safe` (1383-1431): untouched, except the one pinned
  hardening commit (§2 P2).
- **Wiring:** `__init__.py:66` → `controllers=(CoolingController(), night)` —
  **NightSilenceController stays LAST** (its Notte-exit one-shot manuale release
  must yield to the band re-taking a bedroom on the same cycle, per the comment
  at __init__.py:64-65).

### 1.3 The composition fixes that land INSIDE it (R2/R3)

**R2 — deviation-space coalescing** (edits `coalesce_phase` control_law.py:381-403
+ the private `regime_pass` body only; no engine, no signature churn elsewhere):

- `coalesce_phase` re-signatured to per-zone deviation dicts:
  `enter_dev[z] = z.temp − min(z.resolved_center, duty_comfort_max − enter_frac·B/2)`
  (the **crossing-center cap**: a coasted room's RUN fires *below* the hard breach
  line), `exit_dev[z] = z.temp − z.resolved_center`.
- RUN when `comfort_breach` or `max(enter_dev) ≥ enter_frac·B/2` (min_off floor).
  REST only when **every ANCHOR room's** `exit_dev ≤ −exit_frac·B/2` (min_on floor).
- **ANCHOR/BANK partition:** only rooms with `resolved_center ≥ base` veto REST;
  energy-banked rooms (`resolved_center < base`, e.g. PV-banked to the floor) ride
  the phase but never gate it — a gain-limited room banked to the floor at the
  ~0-net 34 °C peak cannot deadlock permanent RUN. **Empty-anchor fallback
  (locked):** if every leader is banked, fall back to all-zone exit deviations —
  otherwise "every anchor satisfied" is vacuously true and the house churns
  instant-REST against min_on/min_off.
- **DELETE the `- z.comfort_relax` temp-side subtraction** (policies.py:780 site,
  by then inside `regime_pass`): relax already lives in `resolved_center` via
  `compose_center` (control_law.py:229-230); deviation space would double-count.
- Breach computation (policies.py:781-783) and REST-via-setpoint-never-BLOCCO
  (regime emits only `BLOCCO_RELEASE`, policies.py:791) unchanged.
- Retire the pv_bias×regime code warning (engine.py:1077-1079) into a
  `feature_graph` warning cell (§1.4) — the coordinator now reads the same
  PV-shifted centers the band slams, but the *combination* stays
  live-unvalidated (see §6).

**R3 — REST-quorum / no-progress guard** (again `coalesce_phase` +
`regime_pass`-local):

- `RegimeState` (control_law.py:265) gains `run_start_temps` (captured on the
  rest→run transition).
- Anchor rooms whose temp fell `< REST_QUORUM_EPSILON` over an elapsed RUN
  `≥ min_on` are dropped from the REST veto (measured-progress-based, so it works
  regardless of model convergence). Comfort is untouched: `_comfort_breach`
  (policies.py:346-360, `active_cooling_leaders`-scoped) still forces RUN
  independently, so excluding a hot stuck room from the veto can never strand it
  above `duty_comfort_max`. Ship the epsilon conservative; validate on
  feature_graph telemetry.

### 1.4 Observability — `feature_graph` (R4)

Pure `build_feature_graph(state, duty_state, regime_state, pv_decision, …)` in
`supervisor/planner.py`, called **only** from `_build_plan_view` →
`PlanView.feature_graph` → `sensor.hvac_plan.feature_graph` (recorder-excluded,
like `room_trajectories`). One row per optimizer — `fan_pacing`, `duty_cycle`,
`regime`, `precool`, `free_cool`, `comfort_windows`, `pv_bias`,
`unified_planner`, `shading`, `night` — each `{enabled, active, inert_reason}`.
R2/R3 later extend it with anchor/bank roles and veto/quorum reasons. Populated
even deploy-dark (the plan view already runs every cycle).

---

## 2. The phased build

Dependency-ordered; each release is a small PR train off `main`, full suite +
ruff on the pinned HA 2026.4.3 target, `gh release` per increment.

### P1 — R1 `resolve_center` (identity) → **v0.39.0**
- **Scope:** §1.1. Files: `supervisor/planner.py` (+`CenterResolution`,
  `resolve_center`, `annotate_centers`, re-export in `supervisor/__init__.py`),
  `supervisor/model.py` (ZoneSnapshot fields), `engine.py` (annotate call after
  :755; `_center_compositions` :911-953 rewritten to read annotated fields),
  `policies.py` (band center block :489-511 → `z.resolved_center`).
- **TEST GATE (this path is LIVE — fan_pacing+duty ON):**
  (a) new `tests/test_resolve_center.py` **golden matrix**
  `{pv bank/coast/hold/None} × {precool T/F} × {comfort_relax 0/1.0} ×
  {eligible T/F} × {schedule fresh/stale/None} × {mode comfort/deep-setback}`
  asserting `resolve_center` == the direct `planner_ref`+`compose_center` wiring
  it replaces (precedence + single clamp site);
  (b) **engine-level end-to-end pre-cool pin** (the function matrix cannot see
  wiring order): fan_pacing+duty on, hot forecast injected ⇒
  `climate.set_temperature == (base − precool_offset) − slam`;
  (c) engine ordering pin: resolution reflects `pv_mode`/schedule (annotate AFTER
  `_pv_bias_apply`/`_maybe_refresh_schedule`);
  (d) loud-fallback pin: no eligible leader ever carries `resolved_center None`
  on an actuating pass;
  (e) `test_plan_view_surfaces_center_compositions` attribute-identical;
  full 322 green unchanged.
- **Deploy-dark:** no new entities, no actuation change; pure identity refactor.

### P2 — M1 fold: `CoolingController` alongside → swap (structural, byte-identical) → **v0.40.0**
- **Scope:** §1.2. Commit sequence inside the release:
  1. **Add-alongside:** `CoolingController` lands in policies.py with the trio
     still present (retained **unwired** as the identity oracle, removed from
     public exports); the three *new safety pins* land here too (see gate d–f).
  2. **Swap:** engine rewiring (§1.2 "Engine after"), `__init__.py:66` →
     `(CoolingController(), night)`; the two M1-breaking engine tests re-targeted
     **in this same commit**: `tests/test_engine.py:274-275`
     (`isinstance(c, DutyController)` + `duty._duty` seed → `engine._cooling._duty`)
     and `tests/test_engine.py:425` (`engine._regime_step = …` mock →
     monkeypatch `engine._cooling.regime_pass` returning `({}, BLOCCO_RELEASE)`).
  3. **Hardening (own commit, own pin):** per-lever epoch check in the reconcile
     loop — engine.py:782 becomes `if self._stopped or epoch != self._epoch: break`.
     Behavior-identical in normal operation; closes the post-fail-safe re-assert
     window when a wedged KNX write (`LEVER_CALL_TIMEOUT` 10 s, engine.py:169)
     outlives the fail-safe's lock wait (`_FAILSAFE_LOCK_TIMEOUT` 5 s, :163).
     Test: fail-safe fired mid-loop (epoch bumped) ⇒ no further writes.
- **TEST GATE (the load-bearing one):**
  (a) **differential identity harness** `tests/test_cooling_identity.py`: the old
  trio wired exactly as engine.py:765-778 (regime.step → prepend regime_blocco →
  Duty(state) → FanBand(state, phase_override) → merge_desired) vs
  `CoolingController()(state)`, asserting per cycle: identical merged `Desired`
  **and** identical post-state (`_duty`, `_rs`, `_states`, `_last_fan`) over
  **multi-cycle scripted sequences** across the gate lattice
  `{fan_pacing} × {duty} × {regime_enabled} × {season} × {free_cool} ×
  {night_active×bedroom} × {consenso on/off/unavailable} × {at_peak} × {precool}
  × {temps straddling center±B/2±ε} × {stint/cooloff expiry, breach abort}` —
  **live combo (duty+fan ON, regime OFF) weighted heaviest**, with the four
  MANDATORY sequences from the adversarial verdicts:
  (1) **night-toggle with hysteresis-adjacent temp** — RUN establishes
  `_last_fan=L` → N cycles `night_active` → wake with raw load inside L's
  hysteresis window ⇒ fan == L in both pipelines AND the `_last_fan` key survived
  the night (the nightly Padronale/Gabriele path; drift here also fragments F2b
  k-windows);
  (2) **released-then-disable** — eligible(RUN) → paused (released `BandState`
  stored :527, manuale-off re-emitted every cycle :530-531) → fan_pacing off
  (`_release_all` includes it, dicts cleared) → re-enable (`{}` first cycle);
  (3) **season flap** summer→winter→summer;
  (4) **fail-safe hand-back-then-resume** — seed mid-cooloff duty, release BLOCCO
  externally + bump epoch, resume ⇒ identical BLOCCO opinions until
  `cooloff_until` elapses, never a lingering block.
  (b) **end-to-end A/B oracle that cannot share transliteration bias:** old-wiring
  vs new-wiring engines through `MockConfigEntry` over identical scripted hass
  state, asserting equal **ordered service-call streams** (cheapest form:
  parametrize the existing engine tests over both wirings for this one release);
  (c) at least a subset of harness rows built via the **real `build_house_state`**
  (catches the gate-source swap);
  (d) NEW `__call__`-level pin: duty disabled + regime yielding + band releasing ⇒
  output **contains the exact pair** `BLOCCO_LEVER: BLOCCO_RELEASE`; plus the
  property "the returned Desired ALWAYS contains BLOCCO_LEVER" across the lattice;
  (e) NEW live-combo conflation pin: regime switch OFF + MEDIUM-classifiable load
  + duty mid-cooloff ⇒ merged output is BLOCK and setpoints follow `band_step`
  (no phase_override effect);
  (f) NEW gate-off toggle: MEDIUM-coalescing → any gate switch off for one pass ⇒
  `regime_state == RegimeState()` → gate on ⇒ first decision matches a fresh
  coordinator (gate-off is a reset pass, never a skip);
  (g) wiring-types test: `engine.controllers` types are exactly
  `(CoolingController, NightSilenceController)` — the oracle trio cannot stay
  silently wired;
  (h) grep-gate: `async_fail_safe` / `_restore_presets` / `_release_blocco`
  (engine.py:1323-1431) byte-identical;
  (i) seam tests `test_band_setpoint_beats_house_mode_via_engine` (:344),
  `test_free_cool_forces_bp_and_band_yields_via_engine` (:374),
  `test_duty_off_releases_a_blocked_villa_via_engine` (:712) pass **unchanged**.
- **Deploy-dark:** identical KNX-lever behavior by gate; one documented allowlisted
  deviation (snapshot-consistent gate reads, §4). **This is the release to soak
  longest live before shipping the next.**

### P3 — M2 delete the trio + mechanical test port → **v0.40.1**
- **Scope:** delete `FanBandController`/`DutyController`/`RegimeCoordinator` and
  the harness's old-pipeline arm (the scenario sequences are re-homed as
  CoolingController-only regression tests). Port the ~25 unit tests
  (tests/test_policies.py:358-871, 651-668, 677-717) as **pure renames** onto the
  public sub-passes: `FanBandController()(state, phase_override=po)` →
  `CoolingController().band_pass(state, po)`; `DutyController()(state)` →
  `.duty_pass(state)` (the `c._duty` pokes at :688-710 survive as class-name
  edits — field name preserved); `rc.step(...)` → `.regime_pass(state)`.
- **TEST GATE:** full suite green with ONLY mechanical renames. **Migration review
  rule (hard):** every migrated pin asserts the exact lever VALUE
  (`blocco == BLOCCO_RELEASE` / `BLOCCO_BLOCK`), never mere key-presence — this is
  where invariant 2's executable spec would otherwise silently weaken.
- **Deploy-dark:** no behavior change; deletion only.

### P4 — R4 `feature_graph` (observability BEFORE behavior) → **v0.41.0**
- **Scope:** §1.4. Files: `supervisor/planner.py` (`build_feature_graph`),
  `engine.py` (`_build_plan_view` populates `PlanView.feature_graph`),
  `sensor.py` (surface, recorder-excluded). Read-only; the plan-view regime
  duplicate (engine.py:826-835) still untouched.
- **TEST GATE:** attribute snapshot across the switch-state matrix; deploy-dark
  test (master off ⇒ feature_graph populated, zero service calls); import-graph
  assertion that `build_feature_graph` is referenced only from `_build_plan_view`.
- **Deploy-dark:** additive sensor attribute only.

### P5 — R2 deviation-space coalescing (deliberate change, latent path) → **v0.42.0**
- **Scope:** §1.3 R2. Files: `supervisor/control_law.py` (`coalesce_phase`
  re-signature), `policies.py` (`regime_pass` body: build dev dicts + partition
  from `z.resolved_center` over `active_cooling_leaders`, delete the relax
  subtraction), `engine.py` (retire the :1077-1079 warning note →
  feature_graph warning cell), `STORY_PV_BIAS.md` (caveat at ~93-95 **softened**
  to "mechanism unified at R2, combination unvalidated live" — NOT deleted).
- **TEST GATE:** (a) **equivalence property test** (a relation, not a recorded
  golden): for uniform centers (`resolved_center == base` ∀ leaders,
  `relax == 0`), new deviation-form == old absolute-form on a randomized grid of
  temps/timers (old `coalesce_phase` kept as a test-local reference impl) — proves
  R2 is identity for every configuration that exists live today; (b) anchor/bank
  partition units (banked room rides REST without vetoing; anchor still vetoes);
  (c) **empty-anchor fallback pin** (all-banked house ⇒ all-zone exit deviations,
  no instant-REST churn); (d) crossing-cap pin (coasted room's RUN fires below
  the breach line); (e) single-count-relax pin (construct a relax>0 divergence
  case, assert new behavior); (f) breach-forces-RUN with a banked room
  (invariant 1); (g) migrate the two coordinator tests (:651-668) to the new form.
- **Deploy-dark:** touches only the `OPT_REGIME_ENABLED` path (default OFF, never
  enabled live). Live behavior byte-identical, proven by (a).

### P6 — R3 REST-quorum + classification unification + tail cleanups → **v0.43.0**
- **Scope:** §1.3 R3 (`RegimeState.run_start_temps`, `REST_QUORUM_EPSILON`
  no-progress exclusion) in `control_law.py` + `regime_pass`. Then two separate
  commits: (i) **unify** the plan-view regime computation (engine.py:826-835)
  onto the controller's classification path behind an equality pin
  (plan-view regime == `regime_classified` across the gate lattice) + a
  `sensor.hvac_plan` attribute snapshot before/after — kills the last
  duplication; (ii) **boot-baseline manuale sweep**: extend the startup safe
  baseline (`async_release_blocco`, engine.py:1372-1381, releases only BLOCCO
  today) to mirror the fail-safe's unconditional manuale release
  (engine.py:1412-1418) so a hard-crash reboot into winter can't leave a bedroom
  fan pinned at a stale % (also protects F2b k-learning from unmanaged
  manuale-on windows).
- **TEST GATE:** quorum units (no-progress anchor dropped from the veto;
  progressing room still vetoes; breach-forces-RUN re-pinned with a hot stuck
  room); classification equality pin; boot-sweep test.
- **Deploy-dark:** R3 is regime-path-only; the unification is sensor-value-pinned;
  the boot sweep is fail-open-only (releases, never asserts).

**NOT in this train:** full retirement of the STORY_PV_BIAS "do not co-enable"
caveat (lines ~93-95) — that happens **only after live pv_bias×regime co-enable
validation** (Phase-7 mild-weather checklist, alongside `unified_planner`).

---

## 3. Invariants — every phase (verbatim-able checklist)

1. **Comfort is model-free:** `band_step` hysteresis + `duty_comfort_max` ceiling
   + comfort-breach-forces-RUN over `active_cooling_leaders`
   (policies.py:346-360) — untouched on every path, re-pinned at P5/P6.
2. **Fail-safe fails OPEN:** REST via raised setpoint, never a lingering BLOCCO;
   `async_fail_safe` releases BLOCCO unconditionally + fans AUTO + presets→auto
   + epoch guard (engine.py:1383-1431) **byte-identical** (grep-gated), except
   the P2 epoch-check hardening (own commit, own pin). The explicit
   `{BLOCCO_LEVER: BLOCCO_RELEASE}`-on-disable (policies.py:385-391) is preserved
   verbatim and re-pinned at the merged `__call__` level; **never a silent `{}`**,
   never key-presence-only assertions.
3. **Deploy-dark + independent per-switch opt-ins UNCHANGED:** `fan_pacing`,
   `duty_cycle`, `OPT_REGIME_ENABLED`, `pv_bias`, `unified_planner` each keep
   gating exactly their sub-behavior; nothing actuates until `switch.supervisor`.
   No phase flips any switch.
4. **Stateful timers advance ONLY on actuating passes:** `CoolingController` is
   called only inside `if actuate:`; the #11 plan view runs pure policies only
   and reads `.duty`/`.regime_state` as read-only properties (no `preview()`
   full-step in this tier).
5. **Bedrooms skipped while `night_active`** (verbatim pop, policies.py:476-478);
   NightSilenceController owns them and stays LAST in the controllers tuple
   (__init__.py:64-66 comment preserved). `_last_fan` **survives** the night
   (asymmetry is load-bearing — see §4).
6. **F2b k-learning untouched:** `thermal.observe` every cycle incl. deploy-dark
   (engine.py:740); manuale_on/fan_pct held-window semantics unchanged.
7. **Controllers-first merge** (band setpoint beats house_mode) and
   yield-on-disabled/paused/free-cool preserved (pinned by test_engine.py:344/374).
8. **Structural = byte-identical** at the KNX-lever level wherever R2/R3 don't
   deliberately change it, with exactly ONE documented allowlisted deviation
   (snapshot-consistent gate reads, §4) — deviations are declared and pinned,
   never asserted away.

---

## 4. Risks carried forward (from the adversarial verdicts) — with baked-in mitigations

| # | Risk | Mitigation (baked into the plan) |
|---|------|----------------------------------|
| 1 | **R1 silent fallback erases live pre-cool** — annotate in the wrong `_cycle` slot / eligibility drift ⇒ `resolved_center None` ⇒ band degrades to base center, nothing fails | Engine-level end-to-end pre-cool pin (P1 gate b); loud WARN-once + test that no eligible leader is unresolved on an actuating pass (gate d); ordering dependency documented in the `_cycle` comment block |
| 2 | **`_last_fan` night-survival asymmetry** (policies.py:477 pops only `_states`; `_last_fan` seeds the first post-wake fan level, :544) — a "cleanup" shifts the morning fan one step AND fragments F2b k-windows (the unified-planner gate) | Mandatory night-toggle-with-hysteresis-adjacent-temp sequence in the P2 harness, asserting fan == L and key survival in post-state equality |
| 3 | **Released-zone bookkeeping normalized away** (`BandState('released')` stored :527; manuale-off re-emitted every cycle :530-531; `_release_all` emission set shaped by it) | Mandatory released-then-disable + season-flap sequences pinning exact per-cycle Desired dicts (P2 gate a.2/a.3) |
| 4 | **Gate-source swap is NOT byte-identical** — `state.duty_enabled`/`fan_pacing_enabled` (built at engine.py:488/497) vs hass re-reads (:962-963) across the awaits ⇒ one-cycle divergence on a mid-cycle switch flip | Declared as a deliberate snapshot-consistency deviation (arguably more correct), documented in P2 release notes, pinned by a dedicated test, allowlisted in the harness — never claimed "identical" |
| 5 | **Shared-bias oracle** — harness author and fold author share the same misunderstanding (e.g. skipping `duty_pass` when regime overrides) | duty_pass-ALWAYS-runs is explicit in the `__call__` contract + multi-cycle differential shaped to catch timer divergence; PLUS the end-to-end A/B (old vs new wiring engines, ordered service-call streams) as the one oracle that can't share transliteration bias (P2 gate b) |
| 6 | **Bare BLOCCO index → cycle-aborting KeyError** on a future duty early-return, stranding an asserted block | `.get` + loud `assert BLOCCO_LEVER in duty_out`; property test "output ALWAYS contains BLOCCO_LEVER" across the lattice (P2 gate d) |
| 7 | **`regime_driving`/`regime_classified` conflation fires on the LIVE combo** (duty+fan ON, regime OFF: MEDIUM load would coalesce + RELEASE would defeat a duty cooloff) | Two named fields (never positional); dedicated live-combo pin: regime OFF + MEDIUM load + mid-cooloff ⇒ BLOCK, no override effect (P2 gate e) |
| 8 | **Stale `RegimeState` on gate-off** if the fold "optimizes" the disabled path into a skip ⇒ instant whole-house REST on re-enable | Gate-off is a RESET pass by contract (engine.py:965-966 branch folded verbatim); toggle-sequence pin (P2 gate f) |
| 9 | **Fail-safe lock-timeout re-assert race** (5 s wait < 10 s lever timeout; a wedged cycle resumes its lever loop post-hand-back — `_stopped` never set on master-off) | P2 hardening commit: per-lever `epoch != self._epoch` break at engine.py:782 + test; the only deliberate fail-safe-adjacent change in the tier |
| 10 | **Pin weakening during test migration** (the fail-safe-critical RELEASE pin diluted to key-presence while "making the suite green") | Harness lands green BEFORE any migration commit; migrated pins must assert exact value pairs (P3 rule); the `__call__`-level RELEASE pin lands in the add-alongside commit, before any deletion |
| 11 | **R2 partition validated only by unit tests** (regime path has zero live history) + no-progress epsilon has no measured prior | Uniform-center equivalence property proves live-config identity; feature_graph (landed BEFORE R2, at P4) provides the live telemetry for Phase-7 mild-weather validation; epsilon ships conservative |
| 12 | **pv_bias × regime combination unvalidated** even after R2 unifies the mechanism | Warning converted to a feature_graph cell, not deleted; STORY_PV_BIAS caveat softened at P5, fully retired only after live co-enable validation |
| 13 | **Plan-view vs controller classification divergence persists P2–P5** (engine.py:826-835 vs regime_pass internals) | Deliberately kept byte-identical until P6 (attribution over cleanliness); then unified behind an equality pin + sensor snapshot |
| 14 | **Boot-baseline manuale gap** (hard crash ⇒ `async_release_blocco` :1372-1381 releases only BLOCCO; a pinned fan stays pinned into winter) | P6 boot sweep commit (fail-open-only), kept OUT of the identity-critical releases |

---

## 5. Key design decisions RESOLVED

1. **Sequencing: MERGE-EARLY (R1 → fold → R2/R3), not fix-then-merge, not
   pure-core.** The behavior and safety verdicts nominally preferred
   fix-then-merge — but their reasons (verbatim fidelity, pin-migration risk,
   oracle availability) are fully neutralized by adopting the fix-then-merge
   design's **controller surface** (public `regime_pass`/`band_pass`/`duty_pass`
   with verbatim bodies and preserved field names → all 25 test ports become pure
   renames; the trio stays alive as a differential oracle through P2). The
   cost-benefit verdict's hole in fix-then-merge is, by contrast, **intrinsic to
   its ordering** and cannot be mitigated: doing R2 in the old structure
   re-signatures `coalesce_phase` + `RegimeCoordinator.step` (policies.py:762-765)
   + edits `engine._regime_step` (:985-988) — plumbing that the later fold then
   deletes — and migrates the regime tests and the engine mock (test_engine.py:425)
   **twice**. Both orderings are gated by the same differential harness against
   the same frozen v0.38.0 baseline (the best-validated the project will ever
   have, live since 2026-07-02), so fix-then-merge's "attribution" argument buys
   nothing the harness doesn't already provide. R2/R3 are dead-path changes
   either way; only the fold touches live paths, and it is equally gated in both
   orders. Merge-early builds the R2 seam **once**, inside a private method.
2. **Controller surface: three public verbatim sub-passes + one composing
   `__call__`** (fix-then-merge's shape) — chosen over a monolithic `__call__`
   (mechanical test migration, unit seams for R2/R3) and over the pure-core
   `cooling_step` (see 3).
3. **Pure-core `cooling_step`/`preview()` REJECTED for this tier.** Its
   "invariant 4 by construction" claim was adversarially shown to *widen*
   exposure (frozen dataclass with mutable dicts + full decision path running on
   every dark tick converts a call-graph guarantee into a purity discipline);
   its byte-identity was shown unachievable as specced (dict rebuilds normalize
   the load-bearing `_last_fan`/released-zone asymmetries; `CoolingPriors`
   re-solves what C3 `SupervisorConfig` solved; planner→control_law module moves
   are shim churn); and its best concrete payoff (killing the classification
   duplication) costs ~20 lines inside the merged controller (P6). Deferred to
   Phase 7+ as an optional distillation once R2/R3 have live history.
4. **BLOCCO precedence becomes ONE explicit line** (`regime_blocco if not None
   else duty`) replacing engine list-order — with `duty_pass` ALWAYS running so
   duty timers advance while overridden, exactly as today (engine.py:769-773
   calls DutyController unconditionally; it merely loses the merge).
5. **Regime classification lives in `regime_pass`; the plan-view duplicate stays
   until P6.** Byte-identity of the live sensor trumps cleanup; the pure-core
   design's own spec contradicted itself here (T1b vs T1c) — resolved by
   deferring the sensor-path swap to its own release behind an equality pin.
6. **Gate reads go snapshot-consistent** (`state.duty_enabled`/
   `state.fan_pacing_enabled`) — the one declared, pinned, allowlisted deviation
   from byte-identity (the fix-then-merge design's claim that the two are
   "identical" was verified false across the `await` at engine.py:741).
7. **R2 details locked:** ANCHOR = `resolved_center ≥ base`; **empty-anchor set
   falls back to all-zone exit deviations** (the unpinned degenerate two designs
   missed); crossing cap `min(center, duty_comfort_max − enter_frac·B/2)`; the
   relax temp-side subtraction is deleted (single-count, in the center only);
   regime still emits only `BLOCCO_RELEASE` — REST stays setpoint-enforced.
8. **R2 regression net is a PROPERTY test, not a recorded golden** (relations
   don't rot; it exactly characterizes every configuration live today).
9. **feature_graph lands BEFORE R2** (observability before behavior; R2 needs the
   warning cell to retire engine.py:1077-1079 into).
10. **Two inherited engine holes ride this train** because the tier rewrites the
    surrounding code anyway: the per-lever epoch check (P2, own commit) and the
    boot-baseline manuale sweep (P6, own commit). Everything else in the
    fail-safe cluster stays byte-identical and grep-gated.

---

## 6. What the next session MUST NOT do

- **Do NOT enable anything.** No flipping `OPT_REGIME_ENABLED`, `pv_bias`,
  `unified_planner`, `comfort`, or any other switch — live opt-ins stay exactly
  `supervisor + fan_pacing + duty_cycle + solar`. Enabling regime/pv-co-enable/
  unified_planner remains gated on Phase-7 mild-weather validation + k-convergence
  + owner sign-off.
- **Do NOT touch the fail-safe except to preserve it** (grep-gate) — the sole
  exceptions are the two pinned hardening commits named in §2 (P2 epoch check,
  P6 boot sweep). No redesign of `async_fail_safe`/`_restore_presets`/
  `_release_blocco`, no controller lifecycle hooks, no reset-on-external-release
  "help".
- **NOT Phase 8.** No comfort-in-optimizer, no model-driven comfort decisions —
  comfort stays model-free (`band_step` + ceiling + breach-forces-RUN).
- **Do NOT build the pure-core `cooling_step`/`preview()`** in this tier (deferred,
  see §5.3), do NOT move `house_load_index`/`select_regime` between modules, do
  NOT introduce `CoolingPriors` (extend `SupervisorConfig` if config plumbing is
  ever needed).
- **Do NOT rename the test-visible private surface**: `_duty`, `_states`,
  `_last_fan`, `_rs` keep their names; the no-arg constructor stays.
- **Do NOT "fix" behavioral asymmetries during the fold**: `_last_fan` surviving
  the night, released-zone re-emission every cycle, the B4 transient freeze
  (freeze ≠ reset), `request_run`'s enabled-gated forced pass — all verbatim.
- **Do NOT weaken migrated pins to key-presence**, do NOT let the trio stay wired
  after P2 (wiring-types test), do NOT reorder the controllers tuple (Night LAST),
  do NOT skip the four mandatory harness sequences, and do NOT delete the
  STORY_PV_BIAS co-enable caveat (~93-95) or declare pv_bias×regime validated —
  soften at P5, retire only after live co-enable data.
- **Do NOT deploy mid-train without soaking P2** — v0.40.0 (the fold) is the
  release that must soak longest on the live duty+fan_pacing path before the next
  lands.

---

**Files touched across the train:**
`custom_components/villa_hvac/policies.py`, `engine.py`, `__init__.py`,
`supervisor/planner.py`, `supervisor/model.py`, `supervisor/control_law.py`,
`supervisor/__init__.py`, `sensor.py`, `tests/test_policies.py`,
`tests/test_engine.py`, `tests/test_resolve_center.py` (new),
`tests/test_cooling_identity.py` (new, deleted at P3), `STORY_PV_BIAS.md`,
`MASTER_PLAN.md` (checklist mirror).
---

## 7. How the next session should start

1. Read: this doc top-to-bottom, `CLAUDE.md`, `MASTER_PLAN.md`,
   `STORY_F4C_UNIFIED_PLANNER.md` (boundary precedent), and the code:
   `custom_components/villa_hvac/{policies.py,engine.py,__init__.py}`,
   `supervisor/{planner.py,model.py,control_law.py}`. Re-verify every cited line
   number against the tree before editing.
2. Confirm the live baseline before touching code (read-only):
   `sensor.hvac_levers` = 0, BLOCCO off, integration loaded, v0.38.0.
3. Execute **P1 → P2** (v0.39.0, v0.40.0) as separate small releases: each =
   code + the FULL test gate from §2, ruff clean, CI green on HA 2026.4.3 /
   Py 3.14, small commit + tag + `gh release`. Deploy-dark; flip no switch.
4. **STOP after P2 and check in with the owner**: v0.40.0 (the fold) must be
   deployed and SOAKED on the live duty+fan_pacing path before P3 (the trio
   deletion) lands — the old trio is the differential oracle and stays in the
   repo until the soak is clean.
5. After the owner confirms the soak: execute **P3 → P4 → P5 → P6**
   (v0.40.1 … v0.43.0), same discipline. §6 (MUST NOT) applies to every phase.
6. ASK before any live HVAC write; deploy each release via HACS + restart only
   with owner OK, then verify (integration loaded, zero villa_hvac errors,
   BLOCCO off, `sensor.hvac_levers` healthy, engine ticking).
