# CLAUDE.md — Villa HVAC integration

Context for Claude Code working on the `villa_hvac` custom Home Assistant
integration. Read this first.

## What this is

An **orchestration** integration for the KNX climate system of Villa Pontacolone.
It does NOT replace the KNX thermostats: it supervises them — reads room sensors
+ presence + weather + the heat-pump call signals, and writes back **KNX presets**
(and later fan stages) to implement: occupancy/night setback, window pause,
fan modulation, solar shading, demand coalescing, long-term zone disable, and
anticipatory radiant heating.

Target: Home Assistant **2026.4.3** (**Python ≥3.14.2** — the 2026.4.x line
dropped 3.13; venv + CI must be 3.14). Single instance, config-flow.

## Critical verified facts (don't re-derive; tested live 2026-06-23)

- **The real PdC call is NOT `climate.hvac_action`** — that attribute is only the
  *mode* (cool/heat). The real call signals are KNX binary_sensors:
  - `binary_sensor.ct_consenso_freddo_villa` — cooling call
  - `binary_sensor.ct_consenso_caldo_villa` — heating call
- **CORRECTION (ETS-verified 2026-06-24): fan % is NOT the cooling-demand
  signal.** The real per-room cooling actuator is the fancoil **chilled-water
  valve (EV FAN, on/off)** — see "Cooling actuation" below. The fan runs ~constant
  100% in AUTO (its commanded % only "holds" in MANUAL); the valve cycles to hold
  setpoint. So `fan.percentage>0` ≠ demand; the **valve state (4/7/x)** is demand.
- **The disable lever:** setting a KNX thermostat preset to `building_protection`
  drives the zone off → cooling consenso drops after a **~1–2 min KNX off-delay**
  (how #10 gates a zone). Note presets and the `temperature` setpoint are
  INDEPENDENT on these climates (changing preset doesn't change `temperature`).
- **#3 reborn as fan PACING (was "dropped"; verified 2026-06-27):** the fancoil
  fan is CONTINUOUS (`percentage_step:1`) and in MANUAL (`switch.fancoil_*_manuale`
  ON) it HOLDS the exact set % — KNX does NOT re-assert it (history: held 33 for
  hours, only changed when commanded, survived an HA restart). So fan % IS a
  per-room cooling-RATE lever *in manual*. #3 = pace each room's fan within #9's
  coalesced run (steady % sized to reach target over the window; two-phase
  approach→maintain past a configurable extended-run threshold). It is the
  per-room EXECUTOR of #9, not a separate feature. (In AUTO the % is noisy and ~100%
  — only manual holds a value; see the manual-override re-assert guardrail below.)
- KNX climates: `hvac_modes: [cool, heat]` (no `off`), `supported_features: 17`
  (target temp + preset), presets `[building_protection, auto, economy, comfort,
  standby]`. Fan speed is a **separate `fan.*` entity**, not on the climate.
- **GOTCHA — season/"are-we-cooling" signal (bit us 2026-06-24):** do NOT gate on
  `sensor.s5a_villa_modo` — it's a *dynamic operating state* that reads `Off` when
  the PdC isn't actively running, so a condition on `== "Raffrescamento"` silently
  fails and the cooling branch is skipped. Use the **thermostat hvac mode**
  (`climate.*` state `cool`/`heat` — local & robust, what #2's season auto-detect
  already does) or `sensor.s5a_stagione` (`Estate`/`Inverno`). This broke the
  legacy `clima_applica_modalita_casa` Via→28 branch; fixed by gating on
  `climate.salotto_termostato_2 == "cool"`. Keep the integration's season-aware
  map on the same robust signal — don't reintroduce `s5a_villa_modo`.

## Cooling actuation — the real signal chain (ETS-verified 2026-06-24)

From `../knx/GroupAddressesReport_2026-03-12` (group 4 VALVOLE):
- **EV FAN water valves = per-room cooling demand/actuator** (on/off OPEN-CLOSE):
  command `4/6/x`, **state `4/7/x`**. Local index x: Salotto 1 · Studio+P1 2 ·
  Rack 3 · Sala Giochi 4 · Cucina 10 · Padronale 11 · Ospiti 12 · Gabriele 13.
  Valve OPEN = that room is actually cooling. **This is the true demand signal**
  (exposed as `binary_sensor.fancoil_*_valvola` via `knx/knx_fancoil_valves.yaml`).
- `consenso_freddo` (2/1/213) ≈ **OR of the EV FAN valves** → PdC chilled water.
- **Central lever: Consenso Freddo BLOCCO `2/2/213`** — force-stop the villa
  cooling call (exposed as `switch.ct_blocco_freddo_villa`). **Polarity VERIFIED
  live 2026-06-30: `on` = BLOCK (consenso dropped to off within ~1 min), `off` =
  ALLOW (consenso recovered) — matches the code (`BLOCCO_BLOCK="on"`).** This is
  #9's real actuator.
- EV HEAT valves (4/0/4/1) are the **winter radiant** testine — not cooling.
- Season changeover per thermostat `7/6/x` (cooling/heating) + global `0/0/5`
  ESTATE / `0/0/6` INVERNO.
- **Mass-bound AND gain-limited at peak (camera_padronale, two tests):**
  - best-case (evening, ~0 sun, fan 100%, setpoint 22): steady **~0.85 °C/h, no
    plateau** (26.6→25.9 / 50 min) → mass-bound, NOT a hard capacity ceiling.
  - peak-sun (2026-06-25 16:00, outdoor 34.5 °C, solar ~300–400 W/m², setpoint 22,
    30 min): **~ZERO net cooling — held 27.1 °C flat** (even drifted up). At peak
    the fancoil only offsets the solar+envelope gain to a draw.
  ⇒ Dominant levers for the hard rooms are **load reduction: solar shading (#6) +
  anticipatory pre-cool (#7)** (bank coolth in cool hours), NOT coalescing/fan
  tricks. (Tests run via a one-shot HA automation; Claude harvest task can't reach
  the connector headless — see [[scheduled-tasks-no-ha-connector]].)

## Summer cooling control plan (valve-based) — supersedes old #3/#9 framing

OBJECTIVE: keep the villa in a summer temperature envelope efficiently (fewer/
longer compressor runs, quieter, mass-aware), driven by the REAL signals —
per-room EV FAN valves + the central Consenso BLOCCO — letting the KNX thermostats
do local bang-bang regulation.

- **Stage 1 — expose signals+lever in HA (KNX yaml).** 8× `binary_sensor` valve
  state (4/7/x) = real demand; 1× `switch` BLOCCO (2/2/213) = central lever. File:
  `knx/knx_fancoil_valves.yaml`; user pastes → reload KNX; then wire entity_ids
  into `villa_hvac` (`cool_valve` per zone, `CONSENSO_BLOCCO`).
- **Stage 2 — measure on the real signals. DONE 2026-06-27** (heatwave window,
  50.2h). Findings: NO compressor short-cycling (consenso = 1 long block/day,
  11–16h, 4 starts/50h); 5 rooms (padronale, gabriele, studio_v, sala_giochi,
  rack) hold their valve OPEN the whole block → gain-limited; only salotto+cucina
  bang-bang the VALVE (~6/h, 192/315 pulses <2min — water-valve chatter, not
  compressor); demand coincident (5–7 valves together, never 1–3); consenso ==
  OR(valves) 99.8%. ⇒ at PEAK coalescing has ~0 headroom (load reduction #6/#7 is
  the lever); in MILD weather demand fragments → sync+rest has headroom. #9 must be
  DUTY-ADAPTIVE, tuned on post-deploy mild-weather data (no mild history yet).
- **Stage 3 — control law (designed; in `MASTER_PLAN.md` + the HTML).** Per-room =
  setpoint/preset (#2, done) + **fan pacing #3** (manual, continuous %). Central =
  **run-planner** (compute window start+duration from house+weather) + **room sync**
  (preset alignment) + **BLOCCO** force-off (envelope-rest/peak/night), comfort
  override + anti-short-cycle. Load = pre-cool (#7) + shading (#6).
- **Stage 4 — implement** in the integration: consume valve binary_sensors as
  demand; coalescing/duty controller actuating BLOCCO; envelope+pre-cool. Opt-in,
  guardrailed, tested, versioned.
- **Stage 5 — validate live + tune.**

## Architecture

- `const.py` — DOMAIN, PLATFORMS, call signals, `FANCOILS`, the verified `ZONES`
  map (zone → climate/fancoils/floor/EP device/emitter), `FAN_STAGES`.
- `coordinator.py` — `DataUpdateCoordinator` (30 s): read-only (fan speeds,
  cooling zones, consenso, fused zone temps). The engine ticks off this.
- `supervisor/` — **pure** core PACKAGE (C2 split, v0.34.0; no HA imports),
  re-exported from `supervisor/__init__.py` so `from .supervisor import X` is
  unchanged. Submodules: `arbiter` (`reconcile` override re-assert + `merge_desired`
  + lever keys) · `control_law` (band/fan, `compose_center`, duty, coalesce, PV
  pre-cool) · `thermal` (F2 RLS + blend) · `model` (`HouseState`/`ZoneSnapshot`/
  `CoverInfo` + leader helpers) · `planner` (run-plan, forward sim, precool, solar
  curve, plan view — **home for the F4c unified planner**) · `returnhome` (#8 core).
  Unit-tested in isolation.
- `engine.py` — `SupervisorEngine`: builds `HouseState` each tick/`request_run`,
  runs the policy stack, applies the merged result via `reconcile` one lever at a
  time (preset/temperature/fan/BLOCCO). Gated by `switch.supervisor`; `async_fail_safe`
  releases BLOCCO on unload.
- `policies.py` — pure preset policies (`disabled_zones` #10 > `window_pause` #4 >
  `house_mode` #2a), priority-merged. `PRESET_POLICIES` registered in `__init__`.
- `controller.py`/`window.py`/`switch.py`/`away.py` — **triggers**: they update
  state (paused set, #10 flag, mode) and call `engine.request_run()`; the engine is
  the single writer. `night.py` #2b is now **`NightSilenceController`, a merge
  controller** (C1, v0.33.0) emitting `{switch:manuale, fan:pct}` opinions through
  the arbiter — no more direct writes; `active` is derived from Notte+setback+wake
  latch (`state.night_active`), so a reboot-in-Notte re-silences via the controller.
- **Band center composition** (F4c Phase 1, v0.33.0): the fancoil band `center` is
  composed by the pure `compose_center` (supervisor.py) — base mode center + AT MOST
  ONE feature (PV bank/coast XOR #9 pre-cool + F4b relax), bounded by a first-class
  **comfort FLOOR** (`OPT_COMFORT_FLOOR`, default house_setpoint−2) symmetric to the
  `duty_comfort_max` ceiling. Named ladder = `COMPOSITION_ORDER` (policies.py);
  per-leader composition on `sensor.hvac_plan.center_compositions` (read-only, incl.
  deploy-dark). The drop-in point the unified planner (Phase 6) replaces.
- `__init__.py` — wires coordinator + engine (policies=PRESET_POLICIES) + the
  legacy controllers; `async_unload_entry`.
- `sensor.py` — diagnostics: `Cooling demand zones`, `hvac_plan` (#11), per-zone
  temp/model, `Energy bias`, and `hvac_levers` (B2: per-lever reconcile decision log
  — state = # levers conceded to manual). `config_flow.py` — single-instance.

Control WRITES through the engine's arbiter (idempotent, manual-override-robust),
never by fighting KNX. **Strict deploy-dark (v0.9.0):** nothing actuates until
`switch.supervisor` is on — on deploy, flip it to light up the migrated #2/#4/#10
at once. The new optimization layer (#5/#6/#9/#7) lands on this same engine.

## Roadmap (incremental, small testable PRs)

1. [x] 0.1 Phase 0 — read-only KPI sensor
2. [x] #10 Long-term zone disable — `switch` per **fancoil** zone;
       off → force `building_protection`, keep frost protection (radiant/split-AC
       zones excluded: lever unverified there)
3. [x] #1 Fused zone temperature — `sensor` per zone, **thermostat-primary**:
       `sensor.clima_*` twin → climate `current_temperature` fallback, 30-min
       staleness. EP NOT used for absolute temp (measured ~5 °C, time-correlated
       bias — see `EP_TEMP_OFFSETS`); reserved for occupancy (#2). TODO: circle
       back to EP-primary with time-varying offset.
4. [~] #2 Occupancy / night setback. Integration owns a house-mode `select`
       (Casa/Via/Notte/Vacanza) → KNX presets comfort/standby/economy/
       building_protection (validated map; replaces legacy
       `automation.clima_applica_modalita_casa`). Also pushes set_temperature =
       `number.villa_hvac_house_setpoint` (dashboard slider) + a SEASON-AWARE
       offset so the integration, not ETS, owns setpoints. Offsets editable in the
       options flow; season auto-detected from the reference thermostat's
       cool/heat state (or forced via the `season` option). setpoint = base +
       offset, so summer (cooling) offsets are POSITIVE and winter (heating)
       NEGATIVE. Defaults: summer Via +5 / Notte +3, winter Via -2 / Notte -4
       (Casa +0, Vacanza none).
       - TODO: per-room comfort override. The single house setpoint flattens
         per-room comfort tuning (matches legacy `temperatura_casa`); add optional
         per-zone setpoint controls (e.g. a number per zone, or a per-zone offset
         from the house base) so rooms can differ.
       Global `Auto setback` switch (default ON); respects #10 (skips disabled
       zones) and #4 (skips window-paused zones).
       - [x] #2a house-mode → preset driver
       - [x] #2b camere silenziose: 2 bedrooms ONLY (Padronale, Gabriele — Ospiti
             is now Studio V office, legacy). Lever = `switch.fancoil_*_manuale`
             + fan off + heat-guard hysteresis; threshold + auto-wake in options
             flow (defaults 26 °C / 08:00). See `night.py`.
       - [x] #2c away auto-escalation (presenza_adulti not_home 18h → Via;
             home → Casa only from auto-Via). Delay in options. See `away.py`.
       (Cleanup TODO: delete the now-replaced HA automations/scripts —
       clima_applica_modalita_casa, clima_backup_via_quando_esco,
       clima_rientro_in_casa, clima_risincronizza, notte_guardia_caldo_camera_*,
       notte_sveglia_automatica_camere, buonanotte/sveglia scripts. Then add a
       startup re-sync so a restart in Notte re-enters camere silenziose.)
5. [~] #4 Window pause — mechanism done (`window.py`): open window → zone cooling
       paused (building_protection) after debounce; close → restore current house
       mode; stays paused across mode changes (apply_house_mode skips paused
       zones). Wired: the 3 `cover.vasistas_*` → their radiant zones
       (vasistas_gabriele is in **bagno_gabriele** NOT gabriroom; bagno_sala_giochi
       → bagno_giochi; vasistas_lavanderia → lavanderia). NOTE: all 3 are radiant
       (no summer cooling to pause — useful in winter); the main cooled fancoil
       rooms have NO window sensor (only those 3 covers + 1 mystery
       `binary_sensor.up_sense_contact` exist). Add a `window` key per zone as
       contact sensors get fitted. Known edge: night heat-guard can still run the
       fan in a window-open bedroom during Notte.
   NOTE — build is now organized as a **Supervisor / single-organism** refactor
   (one arbiter, priority policy stack, idempotent writes) then features as
   policies. Canonical plan: `../hvac-implementation-plan.html`; build checklist:
   `MASTER_PLAN.md`. Phases: 0 (test-pin) → A (supervisor) → B #5 → C #6 → D #9+#3
   → E #7 → F #8 → G deploy. All land as code+tests (not deployed yet).
6. [~] #9 PdC central control → **duty-cycle via Consenso BLOCCO**, syncing the
       villa (all rooms run, then all rest together). Stage 1+2 DONE.
       DONE (v0.12.0): `DutyController` + pure `duty_decision` — cap the
       continuous cooling stint at `OPT_DUTY_MAX_STINT` (consenso_freddo on-time),
       then force BLOCCO block for `OPT_DUTY_COOLOFF`, then release; a zone above
       `OPT_DUTY_COMFORT_MAX` aborts/prevents the cooloff (comfort wins). Opt-in
       via `switch.duty_cycle` (on top of the master). DUTY-ADAPTIVE (v0.13.0):
       at/above `OPT_DUTY_PEAK_OUTDOOR` (30) `duty_decision` releases — don't
       coalesce at peak, let the PdC run + lean on #6/#7. BLOCCO polarity VERIFIED
       live 2026-06-30 (`on`=block, `off`=allow). FORECAST PLANNER (v0.14.0,
       v0.14.1): engine re-fetches the hourly forecast every 30 min
       (`weather.forecast_home`, option override) and `plan_run` builds a plan
       over a LONG `OPT_PRECOOL_LOOKAHEAD_HOURS` (default 12 — thermal mass needs
       a long lookahead). `precool` when a peak ≥ `OPT_DUTY_PEAK_OUTDOOR` is ahead
       AND now is ≥ `OPT_PRECOOL_MARGIN` cooler than it (bank coolth in the cool
       hours, taper as the peak nears → peak-skip). Pre-cool then (a) suppresses
       the duty cooloff and (b) `precool_policy` nudges fancoil setpoints
       `OPT_PRECOOL_OFFSET` colder. Also covers #7's summer pre-cool path.
7. [x] #3 — was two-phase pacing (v0.13.0), **REWRITTEN as comfort-band control
       + capacity-matched fan (#3 v2, F1, v0.17.0)** after the live data showed the
       old pacing rode the valve bang-bang (gated on `z.demand` → released to AUTO
       100% on every valve close) and never touched the kitchen.
       ROOT CAUSE: the KNX thermostat's internal hysteresis is too narrow (~±0.3 °C)
       → valve chatters ~every 2 min near setpoint. FIX = impose our OWN wide
       hysteresis by slamming the setpoint: `FanBandController` + pure `band_step`.
       Per cooling fancoil **leader** zone (it drives all its `fancoils` at one
       speed — living_room owns Salotto+Cucina): RUN drives setpoint to `center−A`
       (valve forced open) + fan at a **capacity-matched** level; REST drives it to
       `center+A` (valve closed) + fan to `fan_min`; flips at `center±B/2`. Long
       uniform cycles, no chatter; fan quiet where load is low. `center` =
       house_setpoint+mode_offset (− precool_offset when #9 says so). Fan % =
       `capacity_fan((G+pulldown)/k)` quantized to 10 levels; `G=cooling_load(a·(T_out−T)
       +b·S+c)` with PRIOR `COOL_*` constants (F2 learns them per room). Pure:
       `band_step`, `cooling_load`, `capacity_fan`, `fan_level`. Engine merges
       **controllers BEFORE pure policies** so the band setpoint beats house_mode
       on its zones (it yields on disabled/paused/free-cool, which the preset
       policies still own). Skips bedrooms while camere silenziose owns them (no
       emit, no fight); releases manuale on disable/season-flip + in `async_fail_safe`
       (fans→AUTO). Settable: `OPT_BAND_WIDTH` (B, 1.5), `OPT_BAND_SLAM` (A, B/2),
       `OPT_FAN_MIN` (global) + per-zone `number.*_fan_min` override (0=off in REST).
       Opt-in `switch.fan_pacing`.
       F2 (online self-refining model) — design+adversarially-reviewed via a
       workflow → 8 small releases v0.18–v0.25 (spec in this session). Model
       `dT/dt = a(T_out−T)+b·S+c − k·u_eff`. F2a DONE (v0.18.0): pure RLS estimator
       (`supervisor.py`: ThermalParams/ParamBounds, `rls_passive_update`,
       `rls_capacity_update`, `estimate_rate` over ≥15min vs 0.1°C/30s noise,
       `blend_params` prior→learned by confidence) + `ThermalEstimator` OBSERVER
       (`policies.py`, ticked by the engine EVERY cycle incl. deploy-dark, returns
       nothing/never actuates; learns {a,b,c} on w=False windows; k decoupled for
       F2b) + `RoomModelStore` (HA Store, best-effort, persist AFTER lever release
       in fail-safe) + `ZoneSnapshot.model_*` (blended, fed to control only in F2b)
       + diagnostic `sensor.hvac_model_<zone>` (G + a,b,c,k + confidence). Opt-in
       `OPT_MODEL_ENABLED` (default on; observer is read-only).
       F2b DONE (v0.19.0): capacity k learned on w=True + HELD-steady-fan windows
       (manuale on + known %, `MODEL_CAP_FAN_STABILITY` spread cap; never AUTO/
       transient) via `rls_capacity_update`; the BLENDED model_* now FEEDS the fan
       sizing (`FanBandController` uses z.model_{a,b,c,k} else COOL_* prior);
       `capacity_fan` gained level hysteresis (`FAN_LEVEL_HYSTERESIS`) to stop fan
       hunting; ZoneSnapshot gained `fan_pct`/`manuale_on`. Until k converges the
       blend returns the prior → fan sizing == F1.
       F3a DONE (v0.20.0): pure house_load_index + select_regime -> regime
       (peak/medium/low) on sensor.hvac_plan (g_house/k_house/load_ratio), read-only
       / deploy-dark; ratio trusted only for converged-k zones, PEAK keys off
       at_peak on priors. No actuation yet (F3c).
       F3b DONE (v0.21.0): pure simulate_room/schedule_precool/build_room_plans
       (reuse band_step/cooling_load/capacity_fan; Euler sub-step guard; fixed-start
       depth grid-scan precool; shared peak_window) -> sensor.hvac_plan.room_plans
       (downsampled, recorder-excluded), PLAN-ONLY.
       F4a DONE (v0.22.0): clear_sky_solar + solar_forecast_curve (sun elevation ×
       clear-sky × forecast cloud, W/m² matching gw3000a) -> replaces the flat-solar
       prior in build_room_plans; opt-in OPT_SOLAR_FORECAST until validated; plan
       solar_model marker.
       F4a-v2 DONE (v0.26.0): the regional weather cloud is UNRELIABLE here
       (validated 2026-07-01: Met.no `weather.forecast_home` said "rainy" at
       gw3000a 1044 W/m²; Forecast.Solar `sensor.power_production_now` tracks the
       daily shape but mis-scales day-to-day). Fix = NOWCAST-ANCHOR: pure
       `solar_curve_v2`/`solar_nowcast_bias` pin the clear-sky×cloud curve to the
       LIVE gw3000a at step 0 and propagate that bias (clamp [0.4,2.5]) forward, so
       CLEAR_SKY_GHI cancels and the curve self-calibrates — the forecast only needs
       the relative SHAPE. gw3000a is the anchor; Forecast.Solar (×FORECASTSOLAR_
       GHI_FACTOR 0.18) is the fallback anchor when the pyranometer is missing.
       Cloud shape still from the OPT_WEATHER_ENTITY forecast (point it at OWM once
       added — better than Met.no). plan `solar_model` marker gains `nowcast`. Still
       opt-in OPT_SOLAR_FORECAST + plan-only (band uses live gw3000a). See
       [[solar-forecast-regional-mismatch]].
       F4b DONE (v0.23.0): per-room/per-fascia comfort windows -> ZoneSnapshot
       comfort_relax raises the band center OUTSIDE the window (capped at
       duty_comfort_max, never a BP slam, never suppresses a breach); bedrooms use
       the night window, day rooms the day window; opt-in OPT_COMFORT_ENABLED.
       F3c DONE (v0.24.0): RegimeCoordinator + pure coalesce_phase/run_rest_durations
       -> in MEDIUM, syncs all leaders RUN/REST together via an explicit
       phase_override into FanBandController (REST closes valves via setpoint, not
       BLOCCO -> fail-safe clean); REST only when ALL rooms cool (a fast room can't
       force-rest a slow one), comfort breach forces RUN, min compressor on/off 10/10
       guardrail; coordinator BLOCCO opinion merged before DutyController (yields ->
       duty survives). Opt-in OPT_REGIME_ENABLED (default off) AND duty AND fan_pacing.
       F4c MPC-lite: DEFERRED (owner). Cross-cutting (from review): identifiability
       gating, hard-room trajectories ADVISORY until k learned (4-param model
       predicts ~0.6°C/h cooling at the verified ~0-net 34°C peak), controllers-first
       merge, recorder-excluded 12h trajectory. 186 tests.
8. [x] #5/#6 Outdoor shutoff + solar shading (Ecowitt `gw3000a_*` + sun + facade).
       #5 DONE (v0.10.0): `free_cool_policy` — summer + `gw3000a_outdoor_temperature`
       below `OPT_FREE_COOL_OUTDOOR` (default 22) → force fancoils to
       building_protection (priority disabled>window>free_cool>house_mode).
       #6 DONE (v0.11.0): `shading_policy` shades a sun-facing cover when summer +
       sun above horizon + azimuth in the facade's band + `gw3000a_solar_radiation`
       > `OPT_SHADING_SOLAR` (200). Covers resolved at runtime (`shadeable_covers`:
       device label=orientation, area→floor, skip orphan/unassigned). Releases (no
       force-reopen) otherwise. Options knob.
       PER-ROOM SHADING (v0.16.0): instead of slamming covers fully shut, the
       policy drives each room's blind to a target HA **position** (0=down,
       100=open) via `cover.set_cover_position`. Per room (keyed by the cover's
       area_id): a `number.*_shade_position` override + a `switch.*_shade_block`
       manual override (blocked room = skipped, not reopened); both auto-created
       per shadeable room (`shadeable_zones`). Fallback = `OPT_SHADING_DEFAULT_
       POSITION` (50, gentle). PROPORTIONAL SHADING (v0.30.0, opt-in
       `OPT_SHADING_PROPORTIONAL`, default off): when on, the fallback becomes a
       solar-scaled depth (pure `proportional_shade_position`: open at the solar
       threshold → deepest = the default position at `SHADING_PROP_SOLAR_FULL`
       700 W/m², + a hot-outdoor boost); a per-room number still hard-overrides.
       Engine cover lever now does set_position (numeric)
       / close (legacy "closed"), reads `current_position`, uses a wider
       `SHADE_POSITION_TOLERANCE`. Verified live 2026-06-30: rooms = main_bedroom
       (grande west + piccola south), office (piccola_studio west), studio_v
       (south); all support set_position (supported_features 15/127).
       #6 cover map is RUNTIME (not hardcoded). Resolver per `cover.*`: zone =
       `entity.area_id` else `device.area_id`; orientation =
       `(entity.labels ∪ device.labels) ∩ {north,east,south,west}`; floor =
       `area.floor_id`; SKIP covers with unassigned/`da_trovare` area (orphan
       `cover.tapparella` → drop, don't crash). A zone may own multiple covers w/
       different orientations (main_bedroom: Grande Camera west + Piccola Camera
       south). Verified 2026-06-27: the 6 cooled-room covers are labeled south/west.
9. [ ] #7 Anticipatory (summer pre-cool live + winter radiant pre-heat) — caldo
       consenso mechanism TBD (behind a flag, verify in heating season)
10. [x] #8 Return-home pre-conditioning (v0.25.0) — was "weekend scenes",
        reframed with the owner: on entering **Via** an actionable notification
        asks *when you're back* (coarse: date + `mattino/pomeriggio/sera`); the
        house sits in **building_protection** (deep setback) until a computed
        pre-cond window, then ramps to comfort so it's ready by arrival (hold &
        wait for presence at the ETA). Implementation = **effective-mode override**
        (NOT new levers): `AwayReturnController.apply` replaces
        `state.house_mode`/`mode_offset` while Via+armed — `Vacanza` (BP, band
        yields to AUTO) while waiting, `Casa` (comfort ramp) inside the window —
        so the whole existing stack follows with zero lever conflict. Pure core in
        `supervisor.py` (`return_eta`/`return_lead_time`/`return_decision` with an
        anti-chatter **latch**); HA wiring in `returnhome.py` (controller +
        `ReturnHomeManager` notification/action). Entities:
        `switch.villa_hvac_return_precond` (opt-in, deploy-dark),
        `switch.villa_hvac_return_armed`, `date.villa_hvac_return_date`,
        `select.villa_hvac_return_daypart`, `sensor.villa_hvac_return_plan`.
        Lead-time is ADVISORY until k converges (gain-limited rooms clamp to
        `OPT_RETURN_MAX_LEAD_HOURS`); fail-safe already covers it (override
        vanishes on unload → native Via). Spec: `STORY_8_RETURN_PRECOND.md`.
11. [x] #11 Plan visualization — DONE (v0.15.0): `sensor.hvac_plan` exposes the
        next-12h PLAN. State = the regime (`pre_cool`/`peak_run`/`duty_rest`/
        `cooling`/`free_cool`/`heating`/`idle`); attributes carry the forecast
        curve + peak (`forecast_peak`/`peak_eta_minutes`/`peak_at`), the duty
        run/rest windows (`stint_start`/`stint_elapsed_minutes`/`cooloff_until`/
        `rest_starts`), per-zone planned setpoints (`zones[].target`), and the
        shading covers (`covers_closing`) — so a dashboard timeline card can
        render the 12h intent. Pure `build_plan` (supervisor.py) + the
        `engine.plan_view` property. CRUCIAL: the plan is computed every cycle
        EVEN WHILE DEPLOY-DARK (engine `_tick`/`_cycle` runs the PURE policies
        read-only; only `_actuate` is master-gated), so the intent is visible
        before actuation lights up. Builds on #9's RunPlan/DutyState/precool. The
        engine now takes pure `policies` + stateful `controllers` separately so
        building the plan never advances the duty/pacing timers.

## Guardrails / domain rules

- **Manual override wins — but detect it ROBUSTLY**: never declare "manual" on a
  single `current != last-written` read. KNX drops telegrams (the salotto write
  loss) and lags attributes (AUTO fan % bounces in sub-second triplets), which look
  identical to a hand change. After writing X, expect X within tolerance (ε on
  setpoint; ignore `unavailable`/`unknown`); if divergent, RE-ASSERT for N cycles
  before concluding; only divergence that survives re-assert → back off. This is
  the #1 robustness risk for a 30 s idempotent writer on this bus.
- **Fail-safe state (define + enforce)**: on unload/crash → release BLOCCO, fans
  AUTO, thermostats local KNX, no lingering building_protection. `async_unload_entry`
  MUST release BLOCCO + hand zones back. Watchdog fails open. Startup re-syncs to the
  safe baseline first. NEVER leave the villa globally blocked without the supervisor alive.
  IMPLEMENTED: `async_fail_safe` releases BLOCCO (unconditional) + fancoil manuale +
  **restores per-zone presets** (B1, v0.31.0: any lingering `building_protection` →
  `auto`, skipping #10-disabled + window-paused). An **epoch counter** invalidates any
  cycle queued behind an in-flight hand-back so it can't re-block/re-slam afterwards
  (esp. the master-OFF path, where nothing else would clear a re-asserted block).
- **Test on the deploy target**: pin `pytest-homeassistant-custom-component` to HA
  2026.4.3 (the venv was stale at 2025.1.4 — ~1yr API drift). CI on target; supervised
  smoke-test on live 2026.4.3 before lighting up any policy.
- **Anti short-cycling**: min on/off durations — don't pump the compressor.
- **Season split**: summer = fancoils (fast, aggressive setback OK); winter =
  radiant floor (slow, high mass → anticipatory, soft setback, not on/off).
- **Kitchen** has no thermostat → follows the Salotto thermostat (open-space).
- **Rack** fancoil cools Rack + Pianerottolo P1 (dual outlet): command =
  P1 demand OR `sensor.rack_t_h_temperature` over threshold.
- **3 split ACs** (Cantina Vini, Palestra, Garage) share ONE compressor → must run
  in the same mode; treat as a synchronized group.
- Bagni Gabri/Ingresso/Palestra + Lavanderia have no EP → fused temp = thermostat only.

## Open questions to resolve

- ~~#3: does HA hold a fancoil fan stage, or does KNX re-assert?~~ ANSWERED
  2026-06-27: with `switch.fancoil_*_manuale` ON, HA holds the % and KNX does NOT
  re-assert (fan is continuous, `percentage_step:1`). No ETS change needed. STILL
  TO VERIFY LIVE (controlled daytime test): does a held LOW % give smooth cooling
  and stop the valve bang-banging? (All manual history so far is nighttime 0/33.)
- ~~BLOCCO polarity (block vs enable)~~ — VERIFIED live 2026-06-30: `on` = block,
  `off` = allow (one supervised toggle; consenso dropped then recovered).
- Heating (`caldo`) consenso mechanism — radiant zone valves, not fan>0. Verify in
  heating season (#7 winter path stays behind a flag until then).
- ~~Per-zone EP temperature offset calibration values~~ — measured 2026-06-23,
  recorded in `EP_TEMP_OFFSETS`; mostly time-correlated so EP-primary deferred.
- ~~Per-zone EP temp/occupancy entity_ids~~ — resolved & filled into `ZONES`
  (`ep_temp`/`ep_occ`). MEDIUM-confidence room matches flagged inline:
  pianerottolo_p2 (7c59ac), bagno_giochi (5a7d68), bagno_padronale_01/02 (shared
  626788). cantina_vini split AC has no `clima_*` twin / no reliable offset.

## Dev / deploy

- **Code** lives here (git repo root = this folder). Push to GitHub; this also
  enables HACS install.
- **Deploy to HA** (HA OS via Nabu Casa): copy `custom_components/villa_hvac/` into
  `/config/custom_components/` (Samba / Studio Code Server App / git pull) and
  **restart HA**; or install via HACS custom repository + restart.
- **Testing**: use `pytest-homeassistant-custom-component` (add a `tests/` + a dev
  requirements file). Lint with `ruff`.
- Replace `CHANGEME` in `manifest.json` (documentation/issue_tracker) and the
  README badges with the real GitHub path; set `codeowners`.
- Note: `__init__.py` uses a classic alias `VillaHvacConfigEntry = ConfigEntry[...]`
  (works on 3.10–3.14); PEP 695 `type` alias also fine on HA's 3.14.

## Background / planning docs

In the parent folder (`Home Assistant/`), authored during design:
- `hvac-restructure-status.html` — system status, gaps, To-Be user stories
- `hvac-implementation-plan.html` — **the build BACKBONE** (rewritten 2026-06-27
  around the Supervisor / single-organism architecture: policy stack, Stage 2
  results, #9+#3 fusion, fail-safe, build phases A–G, live-verify gates). Repo-local
  build checklist mirror: `villa-hvac/MASTER_PLAN.md`.

These were built with the HA MCP connector (live introspection). Live HA control
during dev can stay in that Cowork session; code + git + deploy live here in
Claude Code.

Repo-local review + hardening backlog:
- `ENGINE_REVIEW.md` — critical multi-agent audit of the logic engine (2026-07-01,
  58 verified findings). The **§9-A safety batch is DONE** (v0.29.0, PR #1:
  fail-open BLOCCO, shutdown/boot/master-off hooks, lock-serialized fail-safe,
  BLOCCO never-concede, `_comfort_breach` scope fix, season corroboration, isfinite
  ingest, `asyncio.Lock` cycle serialization). **Do §9-A before enabling
  `duty`/`regime`.** Remaining §B/§C/§D items are tracked in the *Engine-hardening
  backlog* table in `MASTER_PLAN.md`.
