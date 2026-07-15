# Implementation plan — Fix Pack + #3 v3 Steady Governor

**Status:** LOCAL IMPLEMENTATION COMPLETE, 2026-07-15. The complete fix pack and
#3 v3 code train are consolidated in local candidate v0.64.0. Deployment and the
explicit owner-run live validation gates remain external operations.
**Baseline:** live v0.56.0; local candidate v0.64.0; supervisor actuating.
**Companion evidence:** `STORY_PACING_V3_STEADY_GOVERNOR.md`.

## 1. Locked scope

This train delivers, in order:

1. persistent per-bedroom night-silence selection and legacy Buonanotte retirement;
2. correct KNX temperature freshness;
3. a hardware-protection rack guard;
4. thermal-model and fail-safe hardening;
5. a living-room-only steady fan governor, shadowed before actuation.

Implementation record: `steady_pacing` alone is shadow; adding
`paced_living_room` enables actuation. The former band/F3 live path and old
`fan_pacing` entity are retired in v0.64.0, while planner-only pure helpers remain.
F4b is removed end-to-end. `dashboard_v0.64.0_cards.yaml` is the owner-applied
Lovelace fragment because this repository cannot mutate the live dashboard.

Explicitly out of scope:

- **F4c schedule generation is frozen and remains OFF.** The per-room-offset planner
  correction, simulation rewrite, cache changes and activation all move to a dedicated
  later session. Removing F4b from the shared center-composition API is the only
  permitted mechanical seam change; do not otherwise alter F4c behavior. This is a
  hard gate: do not enable `switch.unified_planner`.
- no pacing of stairs/P1 or any gain-limited room;
- no per-room occupancy roster yet;
- no winter behavior changes;
- no automatic pacing enrollment.

## 2. Invariants for every release

- `pytest` and `ruff check` pass on HA 2026.4.3 / Python 3.14.
- New opt-ins restore OFF unless explicitly stated otherwise.
- Master OFF, unload and HA shutdown release BLOCCO, manual fan mode and every
  displaced setpoint; a fan handed to AUTO is physically alive.
- No governor path may call `fan.turn_off` or emit fan percentage 0.
- A failed service call in fail-safe cannot stop the remaining hand-back attempts.
- The thermostat displays the actual effective target being controlled.
- Every behavior-changing release is separately tagged, reviewed and live-verified.
- Existing untracked manuals and `AGENTS.md` are user artifacts; do not modify them.

## 3. Fix Pack releases

### FP1 — v0.57.0: selectable silent bedrooms

**Goal:** replace the old per-room Buonanotte buttons with persistent, safe room
selection while retaining `Notte` as the single trigger.

Implementation:

- Add one `RestoreEntity` switch for each real bedroom:
  - `switch.main_bedroom_night_silence`;
  - `switch.gabriroom_night_silence`.
- Stable unique IDs: `<entry_id>_night_silence_<zone_id>`.
- Default both ON to preserve v0.56.0 behavior; restore the owner's last selection.
- Add a single helper that resolves the switch state for a zone; the
  `NightSilenceController` must not infer selection from entity names.
- `NightSilenceController` manages only selected bedrooms. It must support a
  per-zone release rather than releasing every managed bedroom together.
- Toggling ON during Notte requests an immediate engine pass and enters silence.
- Toggling OFF during Notte performs the full one-zone hand-back immediately:
  `manuale` OFF, restore any guard-nudged setpoint, and explicitly turn the fan ON
  when silence left it OFF. Paused/free-cooling zones keep the existing deferred
  watchdog behavior.
- Morning mode exit and the clock wake release every room that actually participated,
  regardless of the switch's later restored state. Both selected rooms must end with
  `manuale` OFF and a live fan.
- `async_fail_safe` remains the final backstop for both rooms.
- Add the two switches to the climate dashboard in the position formerly occupied by
  the Buonanotte buttons.

Required tests:

- default/restore state for each switch;
- all four selections: neither, padronale only, Gabriele only, both;
- toggle ON and OFF mid-Notte;
- mild-night morning wake re-arms only participating OFF fans;
- guard-active wake does not issue a redundant fan re-arm;
- restart after wake time does not re-silence;
- fail-safe with one or both rooms selected;
- selected room paused/free-cooling at wake follows the existing deferred-rearm rule.

HA-side migration, only after v0.57.0 is deployed:

1. Snapshot YAML for every automation/script/boolean/dashboard item before edits.
2. Remove both duplicate calls to `script.buonanotte_padronale`, retaining the
   `chiudi_notte` press, cooldown and house-mode transition.
3. Strip only climate/manuale/fan/boolean branches from Buonanotte/Sveglia scripts;
   preserve lighting, shutter or unrelated household actions until inventoried.
4. Replace dashboard momentary buttons with the two persistent integration switches.
5. Retire `input_boolean.notte_silenziosa_*` after confirming no remaining consumer.
6. Verify one full night: remote -> Notte -> selected rooms silenced by the supervisor
   only; morning -> participating rooms awake, manuale OFF, fans alive.
7. Keep disabled legacy automations as rollback artifacts for one clean week, then
   delete them. Rewire physical buttons directly to `select.house_mode` last.

### FP2 — v0.58.0: KNX temperature freshness

**Goal:** unchanged cyclic KNX reports remain fresh.

- In coordinator source aging, use `State.last_reported`, not `last_updated`.
- Keep `TEMP_STALE_AFTER` at 30 minutes.
- Preserve the existing primary sensor -> climate fallback and all unavailable,
  non-numeric and non-finite rejection.
- Do not change pure `fuse_temperature`.

Required tests:

- old `last_updated` + fresh `last_reported` is usable;
- both timestamps old is stale;
- unavailable primary falls back;
- a flat temperature reported cyclically for hours never disappears.

### FP3 — v0.59.0: rack hardware guard

**Goal:** protect the rack even when the shared P1 thermostat is satisfied.

Owner-set defaults:

- engage above **28.0 C for 3 minutes**;
- release below **27.0 C for 10 minutes**;
- start the shared fan at **67%**;
- escalate to **100%** after either:
  - rack temperature is at/above 30.0 C for 3 minutes; or
  - 20 active minutes pass without at least a 0.3 C drop from activation temperature.
  Once escalated, hold 100% until release.

Implementation:

- Add `RackGuardController` as the first merge controller. Its opinions win on the
  shared rack/P1 manuale, fan and thermostat setpoint, and it forces BLOCCO RELEASE.
- Add `switch.rack_guard`, restored/default ON, still gated by the master supervisor.
- Add `OPT_RACK_TEMP_THRESHOLD` to options; derive release and emergency thresholds
  from the owner-set threshold unless later made independently configurable.
- While active emit:
  - rack/P1 `manuale` ON;
  - fan 67% or escalated 100%;
  - P1 thermostat target `max(20.0, min(mode_base, p1_temp - 1.0))`;
  - BLOCCO RELEASE.
- Snapshot the un-nudged mode target at first setpoint displacement.
- On release: `manuale` OFF, restore live mode base or snapshot, and ensure the fan
  remains physically ON. Never emit fan 0.
- Yield during winter, Vacanza, P1 disabled/paused/free-air, free cooling, missing rack
  temperature or master/guard OFF; state and alert timers must remain coherent.
- Alert once per episode in Italian if the rack is at/above 30 C for 30 minutes while
  yielded, or while active but ineffective (valve never opens or temperature does not
  respond). Re-arm the alert only after recovery.
- Factor the pure hysteresis primitive out of `night.py` only if both controllers can
  share it without changing night behavior.
- Add rack setpoint restoration to fail-safe and follow the SHA-pin update protocol.
- Surface state, rack temperature, fan command, valve state, nudge target, escalation
  and inert/alert reason on `sensor.hvac_plan.feature_graph` and plan attributes.

Live gate: supervised lowered-threshold or naturally hot test proving that the P1
valve opens, 67% is delivered, escalation works when forced, and release restores the
setpoint/manuale with the fan alive.

### FP4 — v0.59.1: model and toolchain hardening

- Wire `MODEL_W_EDGE_SKIP=3`: after every effective chilled-water state transition,
  clear the estimator buffer and discard the next three coordinator samples before
  starting a new passive/capacity window.
- Keep learned `k` stored, but blend it into control only while `{a,b,c}` is currently
  identified with the existing confidence + solar-excitation gate. Otherwise use the
  prior `k` and expose the inert reason.
- Add raising-path tests that inject service failures at every fail-safe stage and
  prove later releases still run and no exception escapes.
- Pin Ruff exactly and add `pyproject.toml` with Python 3.14 target and the existing
  lint behavior. Do not mass-format or enable `ruff format --check` in this train.

## 4. Steady Governor program

### V0 — owner-run live physics gate; no integration release

Run Salotto+Cucina with normal thermostat target untouched:

- manuale ON and steady 40% for at least 2 hours on a >=32 C afternoon;
- if successful, repeat at 30%; perform one observation on a 38 C-class day;
- record fan, both valves, consenso, temperatures, outdoor/solar and noise verdict.

Automatic aborts:

- living-room temperature above target +0.6 C for 10 minutes;
- comfort ceiling reached;
- temperature or valve data stale/missing;
- Notte, Via/Vacanza, window/free-air pause, free cooling or supervisor shutdown.

Every exit releases manuale and explicitly leaves both fans alive. Success requires
temperature approximately target +/-0.3 C, fewer than 3 valve strokes/hour, acceptable
noise and a clean AUTO hand-back. Failure stops the program and returns to the scoped
band-repair design.

### R0 — v0.60.0: safe structural preparation

- Delete the three unwired legacy oracle controller classes and mechanically re-pin
  their tests onto the shipping `CoolingController` contracts.
- Delete the F4b comfort-window concept completely: control-law branch, options,
  translations, configuration UI, diagnostics and documentation. Stored options are
  ignored. Do not replace it with another schedule.
- Keep current live band/F3 actuation available but OFF for rollback during the shadow
  and first governor soak.
- Apart from mechanically removing the deleted F4b input from the shared center API,
  do not modify F4c or the pure helpers/dormant advisory structures it still imports.

### R1 — v0.61.0: pure steady-governor core

Implement an HA-free state machine and exhaustive tests. Only `living_room` is eligible.

States:

- `NATIVE`: opt-ins/gates off;
- `PACED`: manual steady command candidate;
- `ESCALATED`: immediate safe-direction AUTO hand-back for comfort/data failure;
- `DEMOTED`: native AUTO for the rest of the day after repeated escalation.

Inputs include resolved center, Salotto temperature/error, both living valves, trailing
45-minute living duty and strokes/hour, persistent demand from other rooms, current fan,
kitchen EP slope, data freshness, mode/season and prior state.

House-aware objective:

1. Never use 100% as a normal optimized living-room command.
2. Hold the honest living-room target.
3. If other rooms have persistent demand and the PdC is already committed, choose the
   lowest steady living airflow that holds target.
4. If living is the sole/marginal demand, reduce measured consenso runtime while keeping
   valve strokes under 3/hour; fan percentage is secondary.
5. 100% exists only as a comfort escalation before releasing to native AUTO.

Use a debounced house context, not a single valve sample:

- `SHARED_CALL` when another enabled leader has >=80% valve duty over 45 minutes;
- `MARGINAL_CALL` otherwise. R2 shadow data may tune the 80% threshold before R3.

Candidate governor law, evaluated every 15 minutes and tuned only through R2 evidence:

- fan floor = `max(20, configured living fan_min)`;
- normal fan ceiling = 70%; V0's best held percentage becomes the first seed;
- `SHARED_CALL`: step down 10% after two stable evaluations; hold/step up when valve is
  nearly continuous and temperature error rises;
- `MARGINAL_CALL`: target 50-80% valve duty with <=3 strokes/hour; step up 10% to reduce
  marginal consenso minutes when duty is high, but step down if short strokes exceed the
  rate limit;
- no valid homogeneous duty window -> hold, never guess;
- every emitted candidate is asserted >=floor and <=70 in normal operation.

Kitchen derivative graft:

- use the kitchen EP temperature only as a rolling rate of change;
- >=+0.4 C over 10 minutes adds one immediate +10% step and blocks downward steps for
  30 minutes; repeated qualifying windows may add another step up to the normal ceiling;
- never use the EP absolute value; stale/missing EP data disables only this graft;
- surface the action in Italian on the room card.

Fast safety ladder:

- target error >=+0.6 C for 10 cycles -> immediate +20 within the normal ceiling;
- target error >=+1.0 C, comfort ceiling reached, stale control temperature/valve, or
  20 minutes at the normal ceiling still >=+0.6 C -> release to native AUTO with fans alive;
- two escalations in 3 hours -> `DEMOTED` until the next local day;
- isolated escalation is dashboard/log only; daily demotion sends one phone notification.

Notte behavior: release living-room pacing to native AUTO, set manuale OFF and explicitly
leave both fans alive. Resume only after returning to an eligible Casa state.

### R2 — v0.62.0: live shadow + explanation surface

- Add `sensor.hvac_room_living_room`; compute every candidate and transition but write
  no lever.
- Add fresh, restored-OFF `switch.steady_pacing` and
  `switch.paced_living_room`; in R2 they arm shadow diagnostics only.
- Do not reuse `switch.fan_pacing`: a fresh identity prevents old restored state or old
  semantics from actuating the new law. Keep the old switch/path available and OFF until
  R4 succeeds.
- The thermostat/card target equals the actual composed center. Remove all hidden target
  concepts. If an explicit future energy feature changes the target, display that value
  and its arithmetic honestly.
- Card state + typed attributes must render one honest Italian sentence containing:
  target, actual, proposed fan, delivered fan, valve duty, strokes/hour, house context,
  kitchen slope/action, last action, next evaluation and inert/escalation reason.

Shadow gate: at least 5 days spanning cooking, hot and milder periods. Formal acceptance:

- no candidate 0%; no normal candidate 100%;
- context classification agrees with valve history;
- kitchen triggers correlate with rapid rises and do not hunt;
- every proposed action is explainable from recorded fields;
- predicted marginal-call action would not worsen matched consenso runtime;
- owner accepts the card/noise expectations before actuation.

### R3 — v0.63.0: living-room-only actuation

- Add the new pacer pass behind BOTH fresh switches; default/restore OFF.
- Only `living_room` can emit. Do not create active pacing switches for other rooms.
- Enforce mutual exclusion in `CoolingController`: when the fresh steady path is
  armed, do not execute the old band pass at all (including its release opinions).
  Test all old/new switch combinations; the fresh path wins without duplicate levers.
- While paced, write the composed center honestly to the Salotto thermostat, hold
  Salotto+Cucina manuale ON and command the same steady percentage to both fans.
- Notte and every other ineligible transition perform the explicit live-fan AUTO hand-back.
- Preserve manual-override reconcile behavior, all v0.56.0 watchdogs and fail-safe.
- Keep the old band/F3 path present but mutually exclusive and OFF as rollback code.
- Keep `pv_bias` OFF; do not silently rewire it to the new governor during this release.
- F4c remains OFF and untouched.

Pre-deploy drills: forced escalation, daily demotion, stale Salotto input, stale kitchen EP,
Notte hand-back, window/free-air pause, manual override, master-off, unload/restart and a
byte-equivalent #2b bedroom night.

### R4 — live soak; no deletion yet

Owner enables `steady_pacing` + `paced_living_room` only. Soak at least 5 days across hot
and mild periods. Compare against July 12-14 and matched native periods:

- living fan never normally reaches 100%; delivered percentage matches command;
- target error <=0.3 C for normal operation;
- valve strokes trend below 20 per 8 hours / 3 per hour;
- `SHARED_CALL` reduces living airflow while other rooms own PdC runtime;
- `MARGINAL_CALL` does not worsen matched consenso runtime and preferably reduces it;
- no fan hunting, stranded fan, override concession or unexplained transition;
- kitchen derivative actions are useful and bounded;
- owner accepts noise and the Italian explanation card.

Any comfort or lifecycle regression: turn both new switches OFF, verify AUTO hand-back,
and remain on v0.56-style native control while fixing the isolated governor path.

### R5 — v0.64.0: retire superseded live paths after successful soak

- Remove old `switch.fan_pacing`, RUN/REST band actuation, band slam options/state and
  F3 live regime/coalescing switch/controller path.
- Retain pure F3 helpers and dormant planner advisory structures imported by F4c; their
  cleanup belongs to the later F4c session.
- Keep `run_fan_pct`/thermal model primitives still used for diagnostics or seed advice.
- Rewire `pv_bias` only in a separate reviewed increment after the governor soak proves
  its target contract; it remains OFF until then.
- Update CLAUDE/AGENTS, MASTER_PLAN, README and household manual to the final topology.

## 5. Later, separate F4c session

Do not perform these in this train. The future activation checklist is:

- fold per-room offsets into schedule calculation, diagnostics and cache identity;
- remove deleted comfort/F3 assumptions from the planner;
- rewrite `simulate_room` for native valve hysteresis + steady airflow;
- define the new governor as F4c's reactive comfort backstop;
- verify current living-room `{a,b,c,k}` eligibility and forecast quality;
- shadow the planner for at least one week;
- enable living-room reference only, then evaluate PdC runtime separately.

Until all of those are complete, `switch.unified_planner` stays OFF.
