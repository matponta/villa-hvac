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
  cooling call (exposed as `switch.ct_blocco_freddo_villa`). ⚠️ verify polarity
  (block vs enable) live before actuating. This is #9's real actuator.
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
- `supervisor.py` — **pure** write-arbiter core (no HA imports): `reconcile`
  (manual-override re-assert state machine), `merge_desired` (priority), the
  `HouseState`/`ZoneSnapshot` model, lever-key helpers. Unit-tested in isolation.
- `engine.py` — `SupervisorEngine`: builds `HouseState` each tick/`request_run`,
  runs the policy stack, applies the merged result via `reconcile` one lever at a
  time (preset/temperature/fan/BLOCCO). Gated by `switch.supervisor`; `async_fail_safe`
  releases BLOCCO on unload.
- `policies.py` — pure preset policies (`disabled_zones` #10 > `window_pause` #4 >
  `house_mode` #2a), priority-merged. `PRESET_POLICIES` registered in `__init__`.
- `controller.py`/`window.py`/`switch.py`/`night.py`/`away.py` — now **triggers**:
  they update state (paused set, #10 flag, mode) and call `engine.request_run()`;
  the engine is the single writer. (#2b night fan/manuale still direct, master-gated.)
- `__init__.py` — wires coordinator + engine (policies=PRESET_POLICIES) + the
  legacy controllers; `async_unload_entry`.
- `sensor.py` — diagnostic `Cooling demand zones`. `config_flow.py` — single-instance.

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
6. [~] #9 PdC central control → **run-planner (compute window from house+weather)
       + room sync (presets) + Consenso BLOCCO (2/2/213) force-off**, duty-adaptive.
       Stage 1 (expose valves+BLOCCO) + Stage 2 (measure) DONE. BLOCCO actuation
       gated on a verified-polarity flag.
7. [~] #3 Fan PACING (was DROPPED, now REBORN) — fan is continuous + holds in
       MANUAL (verified 2026-06-27); #3 = per-room fan executor of #9's run.
8. [~] #5/#6 Outdoor shutoff + solar shading (Ecowitt `gw3000a_*` + sun + facade).
       #5 DONE (v0.10.0): `free_cool_policy` — summer + `gw3000a_outdoor_temperature`
       below `OPT_FREE_COOL_OUTDOOR` (default 22) → force fancoils to
       building_protection (priority disabled>window>free_cool>house_mode);
       options knob + enable. #6 shading still TODO.
       #6 cover map is RUNTIME (not hardcoded). Resolver per `cover.*`: zone =
       `entity.area_id` else `device.area_id`; orientation =
       `(entity.labels ∪ device.labels) ∩ {north,east,south,west}`; floor =
       `area.floor_id`; SKIP covers with unassigned/`da_trovare` area (orphan
       `cover.tapparella` → drop, don't crash). A zone may own multiple covers w/
       different orientations (main_bedroom: Grande Camera west + Piccola Camera
       south). Verified 2026-06-27: the 6 cooled-room covers are labeled south/west.
9. [ ] #7 Anticipatory (summer pre-cool live + winter radiant pre-heat) — caldo
       consenso mechanism TBD (behind a flag, verify in heating season)
10. [ ] #8 Interactive weekend scenes (actionable notification)

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
- BLOCCO polarity (block vs enable) — verify with one supervised toggle at deploy.
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
