# MASTER_PLAN.md — build checklist (repo-local index)

**Canonical plan (narrative + architecture):** `../hvac-implementation-plan.html`
("HVAC — Implementation Plan", rewritten 2026-06-27 around the Supervisor /
single-organism architecture). **Verified facts:** [`CLAUDE.md`](./CLAUDE.md).
This file is just the terse build checklist so the repo carries its own pointer.

## Architecture (see HTML for the full spec)

One **Supervisor** (`supervisor.py`, planned) builds a unified house-state model
each 30 s cycle, runs a **priority policy stack**, and writes each lever once
(idempotent diff). Features = policies that *return desired state*, not actors
that call services. Policy priority (high→low):

1. Guardrails — manual-override **write-confirm/re-assert + tolerance**, anti
   short-cycle, frost, split-trio same-mode, #10 disable.
2. #4 window pause · 3. #2 occupancy/mode · 4. #6 shading + #5 outdoor shutoff ·
5. #9 sync+BLOCCO + #7 pre-cool/pre-heat + #3 fan pacing + PV bias · 6. #8 scenes.

Levers: per-zone preset/setpoint/fan%(manual)/cover + global BLOCCO.

**Non-negotiables (from review):**
- **Manual-override robustness:** never declare "manual" on a single
  `current≠written` read — re-assert N cycles + tolerance, ignore
  `unavailable`/`unknown` (KNX drops telegrams; AUTO fan% bounces sub-second).
- **Fail-safe:** on unload/crash → release BLOCCO, fans AUTO, thermostats local,
  no lingering building_protection. Watchdog fails open. Startup re-syncs first.
  *Never leave the villa globally blocked without the supervisor alive.*
- **Test on target:** pin `pytest-homeassistant-custom-component` to HA 2026.4.3
  (venv was 2025.1.4), CI on target, supervised smoke before lighting up.

## Verified levers / signals

- Cooling demand = EV FAN valve (`binary_sensor.fancoil_*_valvola`); consenso ≈ OR.
- Fan is **continuous** (`percentage_step:1`); in MANUAL it **holds the set %**,
  KNX does not re-assert (verified 2026-06-27). AUTO %% is noisy.
- Outdoor/weather: `sensor.gw3000a_outdoor_temperature` + `_solar_radiation` +
  rain/humidity (Ecowitt; richer than `s5a_temperatura_esterna` fallback).
- Sun: `sun.sun` + `input_datetime.sole_in_facciata_dalle`. Season: `s5a_stagione`.
- Central force-off: `switch.ct_blocco_freddo_villa` (polarity unverified).
- **#6 cover map is runtime, not hardcoded.** Per `cover.*` resolver:
  - **zone/area** = `entity.area_id` if set, else `device.area_id`;
  - **orientation** = `(entity.labels ∪ device.labels) ∩ {north,east,south,west}`;
  - **floor** = `area.floor_id`;
  - **skip** covers whose area is unassigned or `da_trovare` (the orphan
    `cover.tapparella` "Tapparella ?" has no area → must be dropped, not crash).
  A zone can own multiple covers w/ different orientations (main_bedroom: west+south).
  Verified 2026-06-27: the 6 cooled-room covers are labeled south/west on the device.

## Stage 2 result (heatwave 50.2 h)

No compressor short-cycling (1 long block/day); 5 rooms run valve continuously
(gain-limited); only salotto/cucina bang-bang (valve, not compressor); demand
coincident (5–7 valves, never 1–3); consenso==OR 99.8 %. ⇒ #9 coalescing only
helps in *mild* weather → duty-adaptive, tune on post-deploy mild data.

## Build phases (each = commit + version + tests)

| Phase | Content | Release |
|---|---|---|
| 0 | Pin test deps → HA 2026.4.3, rebuild venv, CI on target | — |
| A | Supervisor backbone (state model + policy stack + enable switches + guardrails + fail-safe); migrate #2/#4/#2b/#2c to return desired state | v0.9.0 |
| B | #5 outdoor shutoff | v0.10.0 |
| C | #6 solar shading (cover/orientation/floor resolved at runtime from registries) | v0.11.0 |
| D | #9 sync + BLOCCO + fan pacing (#3 fused; BLOCCO behind verified-polarity flag) | v0.12.0 |
| E | #7 anticipatory (summer pre-cool live, winter heat behind flag) | v0.13.0 |
| F | #8 scenes | v0.14.0 |
| F2 | #11 plan visualization — `sensor.hvac_plan` (state=regime; attrs=forecast curve+peak, duty run/rest windows, per-zone setpoints, shading). Pure `build_plan` + `engine.plan_view`; computed every cycle even deploy-dark (pure policies run read-only). ✅ | v0.15.0 |
| #6′ | Per-room shade position + block override (set_cover_position) ✅ | v0.16.0 |

## Self-refining model layer (workflow-vetted F-plan; ALL shipped, deploy-safe)

Designed + adversarially reviewed via a Workflow (4 designers → reviewers → synthesis),
sequenced into small gated releases. All opt-in / observe / plan-only except F1.

| Phase | Content | Release |
|---|---|---|
| F1 | #3 v2: comfort-band setpoint control (wide hysteresis, kills valve chatter) + capacity-matched fan; salotto+cucina one unit; opt-in `fan_pacing` | v0.17.0 |
| F2a | Online passive estimator {a,b,c} (RLS observer, learns deploy-dark); `RoomModelStore`; `sensor.hvac_model_<zone>` | v0.18.0 |
| F2b | Learn capacity k (held-fan windows) → blended model feeds fan sizing + level hysteresis | v0.19.0 |
| F3a | Regime peak/medium/low on `sensor.hvac_plan` (diagnostic; ratio gated on k-convergence) | v0.20.0 |
| F3b | 12h per-room forward sim + grid-scan precool → `room_plans` (recorder-excluded); plan-only | v0.21.0 |
| F4a | Solar forecast (sun elev × clear-sky × cloud); opt-in `solar_forecast_enabled` | v0.22.0 |
| F4b | Per-room/per-fascia comfort windows (capped center relax, never BP slam); opt-in `comfort_windows_enabled` | v0.23.0 |
| F3c | Demand coalescing (MEDIUM regime sync via phase_override; REST via setpoint; min-on/off 10/10); opt-in `regime_enabled` | v0.24.0 |
| F4c | MPC-lite receding-horizon optimiser | DEFERRED (owner) |
| G | Deploy v0.24.0 to live + tune on data + live-verify gates + retire legacy | v1.0.0 |

Cross-cutting (from review): identifiability gating (k vs {a,b,c} on disjoint windows);
hard-room trajectories ADVISORY until k learns (4-param can't reproduce ~0-net cooling
at 34°C peak — comfort always guaranteed by the live band); controllers-first merge;
recorder-excluded trajectory. LIVE = old v0.16.0 still (see NEXT_SESSION.md); deploy
v0.24.0 to light up the model.

## Post-v0.24 backlog (reordered 2026-07-01, owner review)

Gap analysis vs the original 9 user stories + new owner asks. LIVE = v0.24.0
deployed 2026-06-30; F1 band control verified (chatter killed). Detailed #8 spec:
[`STORY_8_RETURN_PRECOND.md`](./STORY_8_RETURN_PRECOND.md).

| Pri | Item | Kind | Notes |
|---|---|---|---|
| 1 | **#8 Rientro & pre-cond** (v0.25.0) | build | effective-mode override (Vacanza↔Casa) while Via+armed; date+daypart, push azionabile, dashboard module. Spec locked. |
| 2 | **Solar forecast v2** (`OPT_SOLAR_FORECAST`) | **DONE v0.26.0** (F4a-v2) | Validated 2026-07-01: Met.no `weather.forecast_home` unreliable (rainy at 1044 W/m²); Forecast.Solar tracks shape, mis-scales day-to-day. Fix = **nowcast-anchor** the curve to the live gw3000a (`solar_curve_v2`), Forecast.Solar fallback anchor, CLEAR_SKY_GHI→1000. Plan-only + opt-in. TODO(owner): add the **OpenWeatherMap** integration (key via HA UI, NOT chat) + point `OPT_WEATHER_ENTITY` at it for a better cloud shape; then enable OPT_SOLAR_FORECAST. |
| 3 | **PV/energy-aware pre-cool (F4c-lite)** | **DONE v0.28.0** | Bank coolth in the thermodynamically most effective hours (model×forecast) using the daily solar-vs-consumption balance; opt-in `switch.pv_bias` (needs fan_pacing+summer), band-center only (no BLOCCO/duty), comfort hard-bounded, parametric floors (22/23, revise for winter). 24-agent review hardened (UTC→local, real-solar gate, min-dwell, COAST comfort-window, degenerate-solar guard). Spec: STORY_PV_BIAS.md. Do NOT co-enable with regime yet. |
| 4 | **Proportional shading** | **DONE v0.30.0** | opt-in `OPT_SHADING_PROPORTIONAL`: shade DEPTH ∝ solar (+ hot-outdoor boost) via pure `proportional_shade_position` — open at the threshold, ramps to the default (deepest) at `SHADING_PROP_SOLAR_FULL` (700 W/m²); a per-room number still hard-overrides. Deploy-dark (default off). |
| 5 | **Enable comfort → regime opt-ins** | enable+tune | after k converges; one at a time, tune on data. |
| 6 | **Second AC circuit (split trio)** | build | Palestra/Cantina/Garage same-compressor group, own setpoints/params. |
| 7 | **Window contacts (cooled rooms)** | hardware+build | mount contacts, wire the `window` key in ZONES → completes #4. |
| 8 | **Season changeover + heating** | seasonal | auto changeover; live-verify caldo. |
| 9 | **#7 winter pre-heat (radiant)** | seasonal | caldo mechanism unverified until heating season. |
| 10 | **#6 winter open-for-gain** (SO + Ovest-P2) | seasonal | passive solar admit in winter. |
| 11 | **EP-primary temp fusion?** | investigate | decide EP-primary (time-varying offset) vs occupancy-only (#1). |
| 12 | Startup re-sync (re-enter camere silenziose after reboot in Notte) | cleanup | |
| 13 | Delete 9 legacy automations → **tag v1.0.0** | cleanup | |

## Engine-hardening backlog (from `ENGINE_REVIEW.md`, audit 2026-07-01)

Critical multi-agent audit of the logic engine (58 verified findings). Full report +
prioritised plan §9 in [`ENGINE_REVIEW.md`](./ENGINE_REVIEW.md). **§9-A (the safety
batch that MUST precede enabling `duty`/`regime`) is DONE** — v0.29.0, branch
`harden/failsafe-blocco`, PR https://github.com/matponta/villa-hvac/pull/1
(fail-open BLOCCO + shutdown/boot/master-off hooks + lock-serialized fail-safe;
`allow_override=False` for BLOCCO + explicit duty-disable release; `_comfort_breach`
scoped to `active_cooling_leaders`; season corroborates `s5a_stagione`; `isfinite`
on all numeric ingest; `asyncio.Lock` serialising `_cycle` + cancellable tick +
`_stopped` guard). Remaining, tracked for future sessions:

| Pri | Item (§ / finding) | Kind | Notes |
|---|---|---|---|
| B1 | **Fail-safe restores per-zone presets** (`failsafe-leaves-bp`) | **DONE v0.31.0** | `async_fail_safe` un-sticks any zone left in `building_protection` → neutral `auto` preset (skips #10-disabled + window-paused, which SHOULD stay BP); guarded, inside the lock. Setpoint restore left out of scope (no native baseline). **Also fixed DEFECT-1** (found in adversarial review): a cycle queued behind an in-flight fail-safe could re-block/re-slam AFTER the hand-back on the master-OFF path (engine alive, `_stopped` never set) → stranded block. Closed with an **epoch counter** (`async_fail_safe` bumps it; a cycle captures it before queuing and aborts if it changed). |
| B2 | **`sensor.hvac_levers` decision log** (`no-reconcile-decision-observability`) | **DONE v0.30.0** | `sensor.hvac_levers`: state = count of levers conceded to a manual override; attrs = per-lever `note`/desired/current/written/attempts/override_until. Engine keeps `lever_decisions` (rebuilt each actuating cycle) + logs a WARNING on the transition INTO `override`. |
| B3 | **Engine-seam integration tests** (`cycle-orchestration-untested-seams`, `regime-coalesce-engine-untested`) | **DONE v0.30.0** | 3 tests drive the full POLICIES+controllers stack through `_cycle`: band setpoint beats house_mode (`[*ctrl,*pure]`), free-cool forces BP + band yields, regime RELEASE beats duty BLOCK. Each verified to fail when its seam is broken. |
| B4 | **State-robustness leftovers** | **DONE v0.32.0** (F4c Phase 0) | consenso `unavailable` → DutyController FREEZES the stint (no reset) + ThermalEstimator skips the window (also bars a k-window on a transient blocco = `observer-blocco-read-poisons-k`); `_forecast`/`_cloud` sorted in `_maybe_refresh_forecast`; cover `_read_current` derives 0/100 from open/closed when `current_position` is absent; `STALE_TEMP_CYCLES` diagnostic (WARN once + `sensor.hvac_plan.stale_temp_leaders`). |
| C1 | **NightController → arbiter controller** (`night-second-writer`) | **DONE v0.33.0** (F4c Phase 1) | #2b is now `NightSilenceController`, an engine merge controller emitting `{switch:manuale, fan:pct}` opinions (silence = manuale on + fan 0 via the arbiter). `active` is DERIVED (Notte + Auto-setback + wake latch) in `build_house_state.night_active`, so a reboot-in-Notte re-silences via the controller; `apply_house_mode` dropped its enter/exit branch. Placed AFTER FanBand in the merge (FanBand wins the Notte-exit hand-back). |
| C2 | **Split `supervisor.py`** (1452 lines / 8 concerns) | **DONE v0.34.0** (F4c Phase 2) | `supervisor/` pure package: `arbiter` (LeverState/reconcile/merge/lever-keys) · `control_law` (band/fan/compose_center/duty/coalesce/PV) · `thermal` (RLS/blend) · `model` (HouseState + leader helpers) · `planner` (RunPlan/sim/precool/solar/plan-view — home for `plan_center_schedule()`) · `returnhome`. No-HA-imports verified; full surface re-exported from `__init__` (importers unchanged); 287 tests green unchanged. |
| C3 | **`SupervisorConfig` parsed-once dataclass** (`options-parsed-ad-hoc`, `housestate-flag-explosion`) | **DONE v0.35.0** (F4c Phase 3) | `supervisor_config.py`: `SupervisorConfig.from_options()` coerces+clamps every option ONCE/cycle (clamps mirror the options-flow ranges); killed ~70 scattered `float(entry.options.get(...))` in the engine (build_house_state + plan-view + regime + pv-bias + solar all read `state.config`). Split realised as `HouseState.config` (the clean config half; the planner reads it duck-typed). Lives at the villa_hvac level so the pure `supervisor/` package stays HA-import-free (model.py refs it under `TYPE_CHECKING`). |
| C4 | **Explicit opt-in dependency graph** (`triple-nested-optin-gating`) | refactor | regime needs duty AND fan_pacing AND regime, gate duplicated; collapse to one "optimise cooling" switch or model the dependency in one place + surface *why* a feature is inert. |
| C5 | **Perf/robustness nits** | **DONE v0.32.0** (F4c Phase 0) | `shadeable_covers` resolved once + cached, invalidated on entity/device/area registry-updated events (no 30 s full scan); every lever `_call` wrapped in `asyncio.wait_for(LEVER_CALL_TIMEOUT)`; `astral` import hoisted to module top (guarded). |
| D1 | **Thermal identifiability gating** (`abc-convergence-summer-scarcity`) | **DONE v0.36.0** (F4c Phase 4) | `ThermalParams.s_hi` tracks max window-mean solar over passive windows (b excitation); pure `abc_identified` (confidence crossed AND s_hi ≥ `MODEL_SOLAR_EXCITATION_MIN`) + `planner_eligible` (abc identified AND k converged). Surfaced per-room: `sensor.hvac_model_*` (solar_excitation / abc_identified / planner_eligible) + `ZoneSnapshot.model_planner_eligible` (ready for Phase 6). Hard-room k documented as a NIGHT-CALIBRATED LOWER BOUND → those rooms stay planner-ineligible → ADVISORY. **No actuation change** (the live blend/fan sizing is untouched; this gates the later planner phase only). |

> **NOTE (2026-07-02):** B4/C1–C5/D1 are now **sequenced into the F4c unified-planner build** —
> see [`STORY_F4C_UNIFIED_PLANNER.md`](./STORY_F4C_UNIFIED_PLANNER.md). Owner decided (after a
> design judge-panel) to build the unified 12 h forecast planner (Track A composition-contract +
> Track B reference-schedule) as ONE program, folding these hardening items in as prerequisites
> (C2 planner module, C3 config, C4 opt-in graph, D1 identifiability gate, C1 unify-writers,
> B4/C5 input hardening). The planner emits a **reference only**; the reactive band keeps the
> model-free comfort guarantee. TRUE F4c (comfort inside the optimizer) stays a gated **non-goal**.
> A fresh session executes the build; this session only produced the plan.

## Live-verify gates (supervised, at deploy — never headless)

BLOCCO polarity · held-low-fan% cooling/valve test (#3) · mild-weather valve
history (#9 tuning) · winter `caldo` mechanism (#7). (#6 cover/orientation map is
runtime registry-resolved + verified — no longer a gate; just confirm all relevant
covers carry an orientation label.)
