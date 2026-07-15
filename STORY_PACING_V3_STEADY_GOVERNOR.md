# STORY — #3 v3 "Steady Governor" (fan-pacing major rework)

**Status:** LOCAL IMPLEMENTATION COMPLETE in v0.64.0 (2026-07-15); live shadow/soak
acceptance remains an owner-run deployment gate. `steady_pacing` ON with
`paced_living_room` OFF is shadow; both ON actuates. The old band/F3 live writer is
retired while F4c's pure planning dependencies remain. The executable authority is
`IMPLEMENTATION_PLAN_FIX_PACK_PACING_V3.md`; this story retains the evidence. V0 remains the
physics gate before any governor actuation build.
**Supersedes:** the #3 v2 RUN/REST band law (the 7/13 trial the owner rejected).
**Owner directive (2026-07-15):** focus the rework on rooms that historically are
NOT at peak full-day 100% — kitchen and salotto — *pacing* them. Fix the three
7/13 pains: (1) illegible center-layer stack, (2) REST fan-off looks broken,
(3) comfort windows relaxing an occupiable bedroom all day.

---

## 1. Evidence (live recorder, hot windows 7/12–7/14, 12:00–20:00, outdoor peaks 37.0/34.6/37.9 °C)

Native KNX bang-bang (supervisor ON, fan_pacing OFF), full-resolution valve
(`4/7/x`) history:

| Leader | Valve duty | Cycles/h | Fan (AUTO) | Temp vs setpoint | Verdict |
|---|---|---|---|---|---|
| **living_room** (Salotto+Cucina) | 21% / 35% / 60% | 6–12/h, median pulse 1.7–2.7 min | follows the valve: 0% closed, 33→100 staircase on open | **pinned at 24.0** (23.98–24.13 all hours) | **HEADROOM — the pacing target** |
| stairs_p1/rack | 54% / ~100% / 100% | **14.4/h** when cycling (worst chatterer) | 100% while pinned | +0.3–0.5 over an aggressive 23.0 | BORDERLINE — secondary target |
| main_bedroom | ~100% pinned | ≈0.1/h | flat 100% | +1.6 to +2.2 | GAIN-LIMITED |
| gabriroom | 100% pinned | ≈0.1/h | flat 100% | +1.0 to +1.6 (mildest) | GAIN-LIMITED |
| studio_v | 100% pinned | ≈0.1/h | flat 100% | +1.3–1.7 solar-shaped | GAIN-LIMITED (#6 problem) |
| sala_giochi | 100% pinned | ≈0.1/h | flat 100% | +1.3 to +2.0 | GAIN-LIMITED |
| office | 100% pinned | ≈0.1/h | flat 100% | **+2.0 to +2.4 (worst room)** | GAIN-LIMITED |

Key facts:
- **Cucina has no independent behavior** — its valve byte-mirrors Salotto's
  (all 701 events, same timestamps). Pacing living_room covers the kitchen for free.
- **The 7/13 trial accidentally ran the experiment**: 10:40:50–10:53:10, salotto
  fan HELD at 50% → the valve stayed **continuously open 12.3 min** (vs 1.7-min
  chatter pulses before/after) and the room cooled 24.1→23.6 (~3 °C/h at half fan,
  outdoor ~26–27). Smooth valve-open cooling, zero chatter. Too short (<15 min) for
  a converged capacity fit, but the strongest direct support for steady pacing.
- **NEW behavioral fact (correct CLAUDE.md when building):** in AUTO the fan % is
  NOT constant-100 for a *cycling* room — it follows the valve (0% while closed,
  sub-second 33→100 staircase on open, ~96 ramps on 7/13). Constant-100 only holds
  while the valve is pinned open.
- Exclude padronale 7/12 daytime from any model fit (dead-fan incident, valve
  interlocked shut).

## 2. The design (judge synthesis: Steady Governor core + grafts)

Three independent designs were produced (steady governor / band repair-in-place /
glass-box pacer) and adversarially judged. **Winner: Steady Governor (35/40)** with
grafts from the runners-up. Full scoring + rejected ideas in §6.

### Mechanism — no slam, the valve is the regulator, the fan is the rate lever
Delete the RUN/REST phase machine and the ±A setpoint slam for paced leaders.
Per paced leader:
- `manuale` ON on every fancoil unit (living_room drives Salotto+Cucina at one %),
- fan held at ONE steady capacity-matched % `u`,
- thermostat setpoint written **honestly to the composed center** (no slam, ever —
  the wall displays the true target; fixes the wall-display half of pain 1).

The KNX thermostat's native ±0.3 bang-bang keeps regulating via the EV valve;
matched capacity should stretch open pulses from ~1.7 min toward 7–16+ min. The
desired duty is context-dependent: high/gentle while another room already owns the
PdC call, lower when living_room is the marginal call and reducing consenso runtime
matters. R2 shadow data, not an assumed universal duty target, freezes the thresholds.

Per-leader states: `PACED` | `ESCALATED` (released to AUTO, fan alive) |
`DEMOTED` (native for the local day after repeated escalation) | `RELEASED`.
Only living_room is eligible. Notte explicitly releases Salotto+Cucina to AUTO with
both fans alive; it never holds a circulation floor overnight. `pacer_pass` is
introduced behind fresh opt-ins, shadowed first, and replaces `band_pass` only after
the owner accepts the shadow behavior.

### Selection — explicit living-room-only opt-in
Use fresh, restored-OFF `switch.steady_pacing` +
`switch.paced_living_room`; do not reuse the old `switch.fan_pacing` identity.
No active pacing switch is created for another room in this train. The gain-limited
rooms stay native #2a + AUTO; stairs/P1 is explicitly excluded because the new rack
guard owns the same fan/thermostat levers. Automatic transitions are safe-direction
only: escalate-to-AUTO and sticky daily demotion with one notification. **No
auto-enroll, ever.**

### Fan law — house-aware objective
- **Priority:** prevent unnecessary living-room 100%, hold target, then minimize
  marginal PdC runtime; valve strokes and fan power are secondary measured costs.
- **Context:** `SHARED_CALL` when another enabled leader is persistently valve-bound
  (initial shadow threshold: ≥80% duty over 45 min), else `MARGINAL_CALL`. A single
  valve sample can never flip context.
- **SEED:** V0's successful held percentage is the initial seed; learned
  `run_fan_pct` remains advice. Normal commands are bounded to
  `[max(20, fan_min), 70]`; 100% is safety escalation only.
- **SHARED_CALL:** the PdC is already owned elsewhere, so step toward the lowest
  steady living airflow that holds target.
- **MARGINAL_CALL:** target 50–80% living valve duty with ≤3 strokes/h; step up to
  reduce marginal consenso minutes when duty is high, but step down when short
  strokes exceed the rate limit. R2 shadow evidence freezes the final thresholds.
- Duty windows with unavailable spans are discarded (hold). Frozen-valve sanity
  freezes adaptation + WARN; the temperature backstop still owns comfort.
- **Kitchen derivative:** the EP kitchen temperature is used only as rate of change.
  A rise ≥0.4 C/10 min adds +10 and blocks downward steps for 30 minutes. Absolute EP
  temperature never enters control; stale EP disables only this graft.
- **FAST ATTACK** (bypasses cadence): fused temp ≥ center+0.6 sustained 10 cycles →
  +20 up to the normal ceiling; ≥center+1.0, or 20 min at the normal ceiling still
  ≥+0.6, or ≥duty_comfort_max → `ESCALATED` to AUTO. Two escalations/3h → native
  for the day + one phone notification; an isolated escalation is card/log only.
- **INVARIANT (asserted in code + tests):** every emitted pct ≥ max(20, fan_min) —
  the pacer path can NEVER write fan 0 / fan.turn_off. The dead-fan/interlock class
  is structurally absent.
- **F2 synergy:** the steady hold is a continuous `rls_capacity_update` window
  (manuale + stable %), so pacing FEEDS k-convergence and D1 planner-eligibility.

### Backstop ladder (model-free at every rung)
(1) the KNX thermostat itself, always regulating at the visible center; (2) temp-only
drift guard (independent of the valve signal); (3) stale fused temp or valve sensor
unavailable → `RELEASED` to AUTO **with a live fan** (note the gabriroom fused-temp
staleness bug is fixed in the fix pack BEFORE this ships); (4) duty_comfort_max
force-escalate; (5) all v0.56.0 nets untouched (stranded-fan watchdog,
`_fans_turned_off`, `async_fail_safe` — the temperature side is clean by
construction: written setpoint == mode center, nothing to restore).

### Center-stack end-state (prune + explain; NOT a rewrite)
`resolve_center`/`annotate_centers` stays THE single live-center site. F4b comfort
windows are deleted completely — control branch, options, UI, diagnostics and docs;
a schedule is not an occupancy proxy. Daytime relax moves to the later occupancy
roster; explicit per-zone trims remain. The wall thermostat always shows the actual
composed center. F4c is code-frozen and OFF in this train; its offset/simulation work
is deferred. `CenterResolution` gains a mandatory human explain string
("24.0 = 24.0 base + 0.0 Casa + 0.0 trim − 0.5 precool → 23.5").

### Explain surface (pain 1)
For this train, `sensor.hvac_room_living_room`: STATE = one word (`paced | escalated |
released | native | gain_limited_exempt | night_silenced | paused_window |
free_cooling`); attributes = typed contract {target(==wall setpoint), actual,
fan_pct, seed_pct, valve_duty_45m, cycles_per_hour, verdict, center explain string,
last_govern_action, next_govern_at, escalation/inert reason} + ONE template-assembled
Italian sentence ("Salotto: tengo la ventola al 40% — valvola aperta il 72% degli
ultimi 45 min, stanza 24.0 su target 24.0"). **Design filter enforced in review: any
behavior that cannot render as one honest sentence from typed fields does not ship.**

### Staged deletions
1. R0 deletes only the unwired trio/oracle classes and F4b in full.
2. The old live band/F3 path stays OFF as rollback through the new governor soak.
3. After R4 succeeds, R5 removes band slam/RUN-REST/F3 live actuation and the old
   switch. Pure helpers/dormant advisory structures imported by F4c remain until
   its separate session.
4. REST fan-off disappears with the old live path.

KEPT: duty_pass/BLOCCO + anti-short-cycle, `run_fan_pct` advice, F2 entire,
resolve_center, all #10/#4/free-cool/night yields, every fail-safe/watchdog/re-arm
net. F4c planner code, including `simulate_room`, is untouched and remains OFF; its
steady-airflow port is a hard gate in the later F4c session.

## 3. Migration plan

The full release contents, tests, aborts and gates are normative in
`IMPLEMENTATION_PLAN_FIX_PACK_PACING_V3.md`:

- FP1 v0.57.0 — persistent per-bedroom night selection, then legacy cleanup;
- FP2 v0.58.0 — `last_reported` temperature freshness;
- FP3 v0.59.0 — rack guard at 28 C, initial 67%, guarded escalation;
- FP4 v0.59.1 — model-edge/k gating, fail-safe raising tests, Ruff pin;
- V0 — guarded 40%/30% living-room physics experiment;
- R0 v0.60.0 — unwired trio cleanup + complete F4b deletion;
- R1 v0.61.0 — pure, house-aware governor core;
- R2 v0.62.0 — at least five days of living-room shadow + explanation card;
- R3 v0.63.0 — fresh-switch, living-room-only actuation with old path retained OFF;
- R4 — live soak and KPI/noise/legibility acceptance;
- R5 v0.64.0 — retire old band/F3 live actuation only after the soak passes.

This staged retirement is deliberate: the highest-blast-radius release has an
immediate switch-off rollback without simultaneously deleting the known path.

## 4. Risks (accepted/watched)

- Low-fan pacing lengthens valve-open pulses but cannot shorten valve-closed time;
  if V0 shows the stroke count doesn't drop enough, fall back to Plan B.
- The valve becomes a control INPUT: frozen/stuck readings are handled by the
  sanity-freeze + temp-only guard, but stuck-open remains a noise (not comfort)
  hazard.
- Kitchen absolute temperature remains untrusted, but a +0.4 C/10 min EP derivative
  gives the shared fan a bounded +10 response and blocks premature step-down.
- Notte releases living-room pacing to AUTO with both fans explicitly alive; there is
  no constant overnight circulation and no governor fan-OFF write.
- Governor cadence (15 min) is deliberately slow; a fast solar ramp is covered by
  the fast-attack rungs, not the governor.

## 5. Interlocks with other work

- `pv_bias` stays OFF and is not silently rewired during R3; a separate post-soak
  increment may connect it to the new governor contract.
- F3c live actuation retires only after R4. Pure helpers used by dormant F4c remain.
- The fix-pack gabriroom staleness fix (coordinator `last_reported`) MUST land
  before R3 (backstop rung 3 depends on staleness meaning "device stopped
  reporting", not "value stopped changing").
- Occupancy roster is the designated future home for deliberate vacant-room relax
  after the complete F4b deletion.
- F4c is explicitly frozen OFF. Offset parity, cache changes, `simulate_room`, model
  eligibility and forecast shadowing belong to a later dedicated session.

## 6. Panel record

Scores (owner_fit/simplicity/safety/migration → total): Steady Governor
9/8/9/9 → **35**; Glass-Box Pacer 8/9/7/7 → 31; Band-repair — lowest (kept as
Plan B). Grafts adopted: per-zone opt-in switches + mandatory center explain string
(Design 2), shadow-mode release + one-sentence card contract + persisted seed
(Design 3), F4b deletion (Designs 2+3 convergent).
Rejected (with reasons, abridged): keeping the RUN/REST slam as default (deliberate
±0.75 sawtooth in the most-occupied room; wall displays slam artifacts — pain 1 at
a surface no sensor reaches); REST-at-20%-circulation (blows unchilled air, valve
deliberately closed — swaps one "broken" for another); rewriting resolve_target /
deleting compose_center+planner_ref (avoidable blast radius on the nightly-critical
`#2a` path; forecloses F4c for zero pacing benefit); learned auto-enroll (override
contention + illegibility); re-basing F3c on BLOCCO (zero live hours, duty already
provides house-level sync); hand-seeding salotto's k from the 12.3-min trial
(unconverged, slammed-setpoint sample).

## 7. Locked owner decisions

1. V0 approved with automatic aborts and a guaranteed live-fan AUTO hand-back.
2. Living_room only; stairs/P1 is excluded because the rack guard shares its levers.
3. Notte releases the living room to AUTO; no constant 20% overnight airflow.
4. Isolated escalation is card/log only; repeated escalation demotes for the day and
   sends one phone notification.
5. Kitchen EP is rate-of-change only: +0.4 C/10 min -> +10 and no down-step/30 min.
6. Remove the comfort-window concept altogether.
7. The thermostat displays the honest composed target.
8. Prevent unnecessary living-room 100% first; then minimize marginal PdC runtime.
   When other heat-bound rooms already own the call, optimize for lower constant
   living airflow instead.
9. Retire F3 live actuation after governor soak; retain pure F4c dependencies.
10. Use fresh restored-OFF `steady_pacing` + `paced_living_room` switch identities.
11. The living-room Italian explanation card and five-day shadow read-through are
    formal acceptance gates.
12. F4c is untouched and OFF for this entire train, including the formerly proposed
    per-room-offset planner fix.
