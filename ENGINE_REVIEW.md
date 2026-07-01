# Villa HVAC — Critical Engine Review

*Audit date: 2026-07-01 · Target: v0.24.0 (deployed live) · Scope: the logic engine
(`engine.py`, `supervisor.py`, `policies.py`, `coordinator.py`) and its control paths.*

Method: 8 independent specialist reviewers (concurrency, fail-safe, arbiter, control-logic,
state-I/O, thermal model, architecture, testing), every finding then re-checked by a separate
skeptic that opened the cited code. 66 raw findings → **58 confirmed, 8 refuted**. This report
keeps the confirmed set, grouped by theme and re-prioritised.

---

## 1. Verdict

The architecture is **sound and unusually disciplined** for a home project: a pure,
unit-tested control core (`supervisor.py`) cleanly separated from an I/O shell (`engine.py`);
a single-writer arbiter with real manual-override robustness; deploy-dark gating; a
fail-safe. The refuted findings confirm the good parts held up (the reconcile release path,
the Euler sub-step guard, the RLS covariance math, the store round-trip were all *checked and
cleared*).

The problems cluster in one place and share one shape:

> **The safety story has holes, and almost every hole is *dormant today* only because the
> `duty` / `regime` / `solar` optimisation opt-ins are still OFF. The moment those switches go
> on — which is the stated roadmap once `k` converges — a cluster of latent BLOCCO/fail-safe
> defects becomes genuinely dangerous (whole-house cooling stranded in a heatwave).**

So the single most important takeaway is a **sequencing rule**: harden the fail-safe / BLOCCO
cluster (§3) *before* enabling `switch.duty_cycle` or `OPT_REGIME_ENABLED`. There are **no
confirmed critical bugs on the live house today**; there are several that become critical on
flip of a switch.

Severity counts (post-verification): **0 critical · 5 high · 14 medium · 39 low.**

---

## 2. The one theme to internalise: BLOCCO is fail-*closed*, and it's treated like any lever

The global `Consenso BLOCCO` switch (`on` = block *all* villa cooling) is the one lever whose
stuck state degrades the entire house. Six independent findings all reduce to: **the code
treats BLOCCO with the same machinery as a comfort setpoint, but its failure direction is
asymmetric — a stuck-RELEASE is harmless, a stuck-BLOCK strands the house.** Every mechanism
that is "respect the human / trust the read / give up after a while" is wrong for this one
lever. Fix them as a set:

| Path | What happens | Finding |
|---|---|---|
| HA shutdown / reboot / OS restart | `async_fail_safe` is registered **only** on `entry.async_on_unload` — it does **not** fire on `EVENT_HOMEASSISTANT_STOP`. A block set before shutdown survives with no supervisor alive. | `no-shutdown-hook` (high) |
| Master switch turned **off** while blocking | `SupervisorEnableSwitch.async_turn_off` releases **nothing**; `_cycle(actuate=False)` never re-touches BLOCCO. The most natural operator action strands the block. | `master-off-no-failsafe` (high) |
| Boot with master off + stale block | Startup only runs `_startup_resync → apply_house_mode` (a no-op while dark) and never reconciles BLOCCO to a safe baseline. | `startup-no-safe-baseline` (high) |
| Transient read at unload | `async_fail_safe` releases only if BLOCCO reads exactly `"on"`; an `unavailable`/`unknown` KNX transient → it concludes "not blocking" and sends nothing. | `failsafe-transient-blocco-read` (high) |
| Dropped release telegrams | Reconcile concedes BLOCCO to "manual override" after 3 re-asserts and then holds hands-off for **2 h** — even when the intended state is RELEASE and the switch is physically stuck on. | `blocco-2h-backoff-safety` / `blocco-override-backoff-stuck-release` (medium) |
| Duty disabled mid-cool-off | `DutyController` returns `{}` (no opinion) instead of an explicit `BLOCCO_RELEASE`; the lever is dropped from `desired` and never actively released. | `duty-reset-loses-cooloff` (medium) |
| Partial platform unload | `async_unload_entry` returns the platform-unload result directly; if it's `False`, the `async_on_unload` fail-safe never runs. | `failsafe-not-run-on-unload-false` (medium) |

**Design principle for the fix:** make BLOCCO **fail-open and unconditional**.
- Fail-safe: *always* send `turn_off` to `CONSENSO_BLOCCO` (idempotent) — never gate on a read.
- Add an `EVENT_HOMEASSISTANT_STOP` listener and a boot-time BLOCCO release **independent of
  the master switch** (deploy-dark should mean "don't optimise", not "don't guarantee the villa
  can cool").
- Have `SupervisorEnableSwitch.async_turn_off` call `async_fail_safe()`.
- Make the reconcile override concession **direction-aware**: when `desired == BLOCCO_RELEASE`,
  never concede — keep re-asserting `off` forever (thread a per-lever `backoff`/`max_reasserts`
  through `_reconcile_lever`, the way cover tolerance already is).
- On disable, controllers that hold BLOCCO must emit an explicit `BLOCCO_RELEASE` once, or the
  engine must actively release any lever it previously wrote that no policy opines on this cycle.

Related: fail-safe hands back the two *global* levers (BLOCCO, `manuale`) but leaves per-zone
thermostats slammed in `building_protection` / the #3 REST setpoint — the startup re-sync
self-heals this on a normal restart but **not** on integration removal/disable
(`failsafe-leaves-bp`, medium; `failsafe-manuale-release-partial`, low).

---

## 3. Concurrency & the single-writer discipline

The whole design rests on "one writer, idempotent, override-robust." Two findings poke holes
in that guarantee:

- **`tick-requestrun-interleave` (medium, confirmed).** `_on_update` checks `self._busy` then
  only *schedules* `_tick()` via `async_create_task` — it does **not** set `_busy`. `_busy` is
  set inside `_cycle`, on a later loop turn. So a `request_run()` awaited in the gap (window /
  mode / switch / number events all do this) can pass its own guard and run a *second* `_cycle`
  concurrently, interleaving at every `await` point over the shared `self._lever_states`,
  `self._forecast`, and the stateful controllers' timers. A bool set *inside* the coroutine
  cannot guard a coroutine scheduled on a prior turn. **Fix:** hold a single `asyncio.Lock`
  across the entire `_cycle` body (immune to the set-then-schedule gap and serialises the
  reconcile writes too). Harmless while deploy-dark; live-exposed now.

- **`fire-forget-task-lifecycle` (medium, confirmed).** Every event handler spawns work via
  bare `hass.async_create_task(...)`; those tasks live on `hass._tasks`, not `entry._tasks`, so
  config-entry teardown has **no drain/cancel net** for them. A `_tick` or `night._run_guard`
  queued microseconds before unload runs *after* `async_fail_safe`, re-blocking BLOCCO or
  re-pinning a `manuale` with no supervisor alive. **Fix:** use
  `entry.async_create_background_task` (HA cancels it on unload) and early-return `_cycle`/
  `_on_update` when stopped (`if self._unsub is None: return`).

Two smaller ones: every lever write is a serialized `blocking=True` service call, so one
wedged KNX call freezes the whole cycle — wrap each in `asyncio.wait_for`
(`blocking-service-call-hang`, low); and `request_run` silently drops event passes while busy —
a `_rerun_pending` flag would coalesce them (`busy-drops-request-run`, low).

*Refuted & cleared:* the fear that `NightController` and the engine write bedroom levers
*concurrently* — they don't (the engine skips bedrooms while `night_active`), though Night is
still a second *writer*, see §6.

---

## 4. Control-logic correctness

- **`comfort-breach-all-zones` (high, confirmed) — the headline logic bug.**
  `_comfort_breach(state)` (policies.py:256) iterates **all** `state.zones.values()` and trips
  if *any* zone exceeds `duty_comfort_max`. But `ZONES` includes radiant-only baths, split-AC
  rooms (Palestra/Garage) and other spaces with **no fancoil cooling** that still get a fused
  temp every cycle. A single chronically-warm uncooled room (a 27 °C bathroom) trips the breach
  every cycle → `duty_decision` takes the `comfort_breach → BLOCCO_RELEASE` branch forever → the
  cool-off never holds → **#9 is silently, permanently neutralised** the moment it's enabled.
  `RegimeCoordinator.step` already scopes its identical breach to `leaders` only (policies.py:611)
  — proving the intended scope. **Fix:** extract one shared `_breach_leaders(state)` helper
  (`_is_cooling_leader` + enabled + not paused + not bedroom-at-night + `temp is not None`) and
  have *both* consume it, so the two can never drift again. (Decide deliberately whether the
  breach should also honour `comfort_relax`, which the regime path subtracts and the duty path
  doesn't — `comfort-relax-can-suppress-duty-breach`, low.)

- **`precool-policy-dead-under-fan-pacing` (low, confirmed).** `precool_policy` writes
  `temperature_lever` and so does `FanBandController`; controllers merge *first*, so whenever
  fan-pacing is on the pre-cool setpoint is dead code (the band already applies pre-cool via its
  `center`). Delete `precool_policy` or restrict it to zones the band controller doesn't manage,
  to remove the phantom double-write.

- **`coalesce-no-deadlock-but-slow-room-blocks-rest` (low).** In MEDIUM, REST is gated on the
  *hottest* room reaching the exit threshold; a single slow/gain-limited room can strand the
  whole house in RUN indefinitely. Add a bounded fallback (if RUN exceeds `max_stint`, drop to
  per-room band or force REST honouring `min_on`).

- **`band-slam-vs-knx-band` (low).** The band-slam amplitude `A` (0.75 °C default) only forces
  the KNX valve if it exceeds the thermostat's own internal deadband — verify `A` ≥ measured
  deadband + setpoint tolerance, or the wide hysteresis silently collapses back to chatter.

- **`tiny-k-fan-pins-100` (low).** A learned `k` near `MIN_K` (0.1) pins the fan at 100% in RUN
  forever (intended, but should surface a `fan_saturated` diagnostic so it's not mistaken for a
  bug).

---

## 5. Sensor / state robustness

- **`nan-inf-passthrough` (medium, confirmed).** Every numeric read (`_num`, `_sensor_temp`,
  `_climate_temp`) is a bare `float(state.state)` with no `isfinite` guard, so a sensor
  reporting `nan`/`inf` poisons `outdoor_temp`, `solar`, `zone.temp`. NaN makes every
  `outdoor >= peak` / `< free_cool` comparison silently `False` (peak protection + free-cool
  quietly disable while looking healthy); `inf` makes `outdoor >= peak` always-True (permanent
  peak lockout). **Fix:** one `math.isfinite(v)` guard per ingest site, mirroring the guard the
  RLS code already has; add a write-side guard on `band_step`'s `center` as defence-in-depth.

- **`season-default-summer-winter-misactuation` (medium, confirmed).** `current_season` returns
  WINTER only if the *single* reference climate reads `"heat"`; `None`/`unavailable`/`"cool"`/
  anything else → SUMMER. One unavailable KNX climate in heating season flips the whole organism
  to summer (positive setback offsets; and once fan-pacing is on, active cooling of a house that
  should heat). This is the exact single-fragile-signal class `CLAUDE.md` warns about for
  `s5a_villa_modo`. **Fix:** treat `unavailable` distinctly (persist + hold last-known season),
  corroborate with `sensor.s5a_stagione` (Estate/Inverno), and suppress cooling actuation when
  the season is indeterminate.

- **`consenso-unavailable-mistaken-notcooling` (low).** `consenso_freddo` is compared `== "on"`;
  `unavailable` silently becomes "not cooling" → resets the duty stint timer and mislabels the
  thermal-learning window. Distinguish unknown from off (freeze the timer, skip the learning
  window).

- **`cloud-sort-assumption` (low).** `_forecast_cloud_at` early-`break`s assuming the cloud list
  is time-sorted, but nothing sorts `self._cloud`. Sort it in `_maybe_refresh_forecast` (or drop
  the break).

- **`shadeable-covers-full-registry-scan` (low, performance).** `shadeable_covers` iterates the
  **entire** entity registry every 30 s (twice, via `shadeable_zones`). Resolve once at setup +
  on a registry-updated listener; cache the tuple.

- **`temp-none-silent-disengage` / `climate-age-uses-state-lastupdated-for-attribute` (low).** A
  leader whose fused temp goes `None` silently drops out of all temperature control with no
  alarm; and staleness for the climate-fallback temp keys off `state.last_updated`, which does
  **not** track the freshness of the `current_temperature` *attribute*. Surface a diagnostic when
  a controlled leader is temp-less for >N cycles.

*Refuted & cleared:* forecast fetch degrades gracefully (no crash); the temperature-attr-None
read is handled correctly by reconcile's transient branch.

---

## 6. Thermal model (F2 RLS) — a reality check

The RLS math itself is sound (the covariance-collapse, sub-step, and store-round-trip fears
were all *refuted*). The real issue is **identifiability in this specific house**:

- **`abc-convergence-summer-scarcity` (medium, confirmed as an efficacy limitation).** `{a,b,c}`
  is learned only on `w=False` (no-chilled-water) windows ≥ 15 min. Stage-2 data shows the 5
  hard rooms hold their valve **open the entire 11–16 h cooling block** in heat — so they get
  essentially zero daytime `w=False` windows and `{a,b,c}` only ever calibrate on cool, sunless
  nights, which never excites the solar coefficient `b`. Then `k` (computed from frozen `a,b,c`)
  is biased low. **And `k` is doubly starved:** it needs a `w=True` **held-steady-fan** window,
  but the hard rooms run AUTO ~100% during the block and rarely produce one. Net: **for exactly
  the rooms that matter, the model may never converge** — so the whole F2 → #9/regime value
  proposition may not land there. It degrades *safely* (prior-dominant blend, comfort held by the
  band), so this is efficacy not correctness. **Fix:** add an identifiability gate (only raise
  `abc` confidence when the fed windows spanned a real solar range); document that hard-room `k`
  is a night-calibrated lower bound; treat deliberate daytime valve-close probing as a research
  task, not a fix.

- **`w-edge-skip-unused` (low).** `MODEL_W_EDGE_SKIP` is defined but never wired — the KNX
  ~1–2 min off-delay contaminates the first sample after a chilled-water edge. Actually use it.

- **`slope-vs-mean-regressor-bias` (low).** Regressing a window *slope* against window-*mean*
  regressors biases `{a,b,c}` when conditions trend within the window.

- **`blend-inconsistent-tuple` (low).** Blending `{a,b,c}` and `k` with *independent* confidence
  weights can produce a physically inconsistent (converged-abc, prior-k) tuple that mis-sizes the
  fan. Gate `k` blending on `abc` also having converged.

- **`regime-vs-band-k-gating-mismatch` (low).** `house_load_index` trusts `k` only when
  converged (≥0.5 conf) but `FanBandController` trusts the blended `k` always — the regime says
  "prior" while the fan sizes off a half-learned `k`. Align the gate or document the divergence.

- **`observer-blocco-read-poisons-k` (low).** The learning window is classified from a possibly
  transient `blocco` read; a wrong read biases the learned `k`. Require a non-transient
  consenso/blocco read before admitting a `k` window.

---

## 7. Architecture & refactor opportunities (the "smarter, cleaner" part)

These are the constructive items — a refactor that hardens *and* simplifies without losing
capability.

1. **Fold the last direct writers onto the arbiter.** `NightController` (`night.py`) still writes
   bedroom `manuale`/`fan` **directly**, bypassing the reconcile arbiter, its manual-override
   detection, and its lever tracking (`night-second-writer`, low). `_startup_resync` reaches
   around the engine into `apply_house_mode → night.enter/exit` (`startup-resync-legacy-path`).
   Convert #2b into a `night_silence` **controller** that emits `{switch:manuale, fan:pct}` lever
   opinions merged like everything else. This removes the last "second organism," makes deploy-
   dark uniform, and lets the startup path be a plain `engine.request_run()`.

2. **Split the `supervisor.py` monolith (1452 lines, 8 concerns) into a pure package**
   (`supervisor-monolith`, low). Along the existing banner lines:
   `arbiter.py` (LeverState/reconcile/merge/lever-keys) · `thermal.py` (ThermalParams/RLS/blend)
   · `planner.py` (RunPlan/plan_run/sim/precool/solar) · `control_law.py` (band/fan/duty/coalesce)
   · `returnhome.py` (already a natural unit). Keeps the "pure, no-HA-imports" property while
   making each unit independently reviewable.

3. **Kill the config sprawl with a parsed-once config object** (`options-parsed-ad-hoc`,
   `housestate-flag-explosion`, low). ~33 ad-hoc `float(entry.options.get(...))`-with-try/except
   sites are scattered across `engine`/`controller`/`policies`. A frozen `SupervisorConfig`
   dataclass with one `from_entry(entry)` classmethod that coerces + clamps every option once per
   cycle would (a) delete the repeated boilerplate, (b) centralise validation, and (c) shrink
   `HouseState`'s 30+ fields to `measured state + config`.

4. **Make the opt-in dependency graph explicit** (`triple-nested-optin-gating`, low). Regime
   needs `regime AND duty AND fan_pacing` all on, with the gate duplicated and a silent reset when
   any is off. Either collapse to one "optimise cooling" switch that owns the whole stack, or
   model the dependency in one place and surface *why* a feature is inert.

5. **Hoist the `astral` import** out of `_solar_forecast` (runs every solar-enabled cycle;
   `astral-import-in-function`, low).

6. **Version/dep hygiene:** `manifest.json` still has `CHANGEME`; no `ruff` config file so CI
   lints with bare defaults (`no-ruff-config-defaults`) — add `[tool.ruff]` (E,F,I,BLE,ASYNC,B;
   `target-version = py314`) and pin an exact ruff version.

---

## 8. Testing & observability

The pure core is well-tested; the **composition seams and the fail-safe branches are not** —
which is exactly where a refactor will silently regress.

- **`no-reconcile-decision-observability` (medium).** `reconcile()` computes a rich per-lever
  `note` (`write`/`reassert`/`override`/`manual-hold`) every cycle and the engine **throws it
  away** — it isn't even logged. On the live house you cannot tell *why* the engine did or didn't
  actuate a lever, and a lever stuck in 2 h override backoff is invisible. Add a diagnostic
  `sensor.hvac_levers` (per-lever: last note, desired, current, attempts, `override_until`) —
  cheap, and it directly serves the #1 documented robustness risk. Add a `_LOGGER.warning` when
  any lever crosses into `override`.

- **`cycle-orchestration-untested-seams` / `regime-coalesce-engine-untested` (medium).** No
  engine-level test instantiates the **full policies + controllers stack** and asserts the merged
  `desired` — so the load-bearing `[*ctrl_outputs, *pure_outputs]` order, "regime BLOCCO beats
  duty," and "band setpoint beats house_mode while free-cool still forces BP" are all unguarded.
  Add 2–3 `_cycle`-level integration tests to lock the ordering before the module split.

- **`failsafe-partial-raise-untested` (medium).** The try/except independence in `async_fail_safe`
  (a raising BLOCCO release must still attempt every `manuale` release; persist must run last) is
  its whole reason to exist and has **zero** coverage. Add tests with an `AsyncMock` side-effect
  that raises.

- **`thermal-convergence-shorthaul-only` / `diag-sensors-untested` (low).** RLS convergence is
  only tested on short synthetic windows (add a 24 h noisy simulation from known `{a,b,c,k}`);
  `HvacModelSensor` and `ReturnPlanSensor` have no tests.

---

## 9. Prioritised action plan

**A. Do before enabling `duty` / `regime` / `solar` opt-ins (safety-gating):**
1. Fail-open BLOCCO everywhere (§2): unconditional fail-safe release; `EVENT_HOMEASSISTANT_STOP`
   hook; boot-time release independent of the master; master-off → `async_fail_safe`;
   direction-aware override (never concede a RELEASE); explicit `BLOCCO_RELEASE` on duty disable.
2. Fix `_comfort_breach` scope (§4) — otherwise #9 is dead on arrival.
3. Fix `season` fallback + add `isfinite` ingest guards (§5).
4. Serialize `_cycle` with an `asyncio.Lock` and use `async_create_background_task` (§3).

**B. Reliability / correctness (soon):**
5. Fail-safe also restores per-zone presets (skip #10-disabled) (§2).
6. `nan-inf`, `consenso-unavailable`, cloud-sort, cover-`closed` type mismatch (§5, low set).
7. Add the `sensor.hvac_levers` decision log + engine-seam integration tests (§8).

**C. Refactor (deliberate, test-backed):**
8. Night → arbiter controller; split `supervisor.py`; `SupervisorConfig` dataclass; explicit
   opt-in graph (§7).

**D. Research / accept-and-document:**
9. Thermal identifiability for the hard rooms (§6) — gate confidence on solar excitation;
   document `k` as a night-calibrated lower bound; keep the band as the comfort guarantee.

---

## 10. What was checked and cleared (refuted)

For confidence: these plausible-sounding concerns were investigated and **do not hold** —
`night`/engine do *not* write bedroom levers concurrently; the `desired=None` release does *not*
wipe override memory prematurely; the temperature-attr-None read is handled; controller-vs-policy
merge does *not* thrash; the Euler sub-step guard uses the right variable; RLS covariance is
*intentionally* not reset on edges (buffer clear is sufficient); `c=0` prior with `MAX_C=3` is
fine; the model store round-trips correctly.
