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

Target: Home Assistant **2026.4.3** (Python 3.13). Single instance, config-flow.

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
- **#3 fan-stage modulation is moot:** the water valve is on/off and the fan is
  constant — there is no per-room "cooling intensity" to modulate. Drop #3.
- KNX climates: `hvac_modes: [cool, heat]` (no `off`), `supported_features: 17`
  (target temp + preset), presets `[building_protection, auto, economy, comfort,
  standby]`. Fan speed is a **separate `fan.*` entity**, not on the climate.

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
- **Thermal mass, not capacity:** camera_padronale best-case (evening, ~0 sun,
  fan 100%) cooled a steady **~0.85 °C/h with no plateau** 26.6→25.9 over 50 min.
  Rooms are mass-bound (soaked at ~26 all day), not capacity-limited → levers are
  anticipatory pre-cool + shading (#6), not fan tricks. (Peak-sun rate: see the
  scheduled 2026-06-25 16:00 test.)

## Summer cooling control plan (valve-based) — supersedes old #3/#9 framing

OBJECTIVE: keep the villa in a summer temperature envelope efficiently (fewer/
longer compressor runs, quieter, mass-aware), driven by the REAL signals —
per-room EV FAN valves + the central Consenso BLOCCO — letting the KNX thermostats
do local bang-bang regulation.

- **Stage 1 — expose signals+lever in HA (KNX yaml).** 8× `binary_sensor` valve
  state (4/7/x) = real demand; 1× `switch` BLOCCO (2/2/213) = central lever. File:
  `knx/knx_fancoil_valves.yaml`; user pastes → reload KNX; then wire entity_ids
  into `villa_hvac` (`cool_valve` per zone, `CONSENSO_BLOCCO`).
- **Stage 2 — measure on the real signals.** Re-run demand analysis on 4/7/x
  (true per-room duty cycle, staggering, valve↔consenso); fold in the
  camera_padronale mass tests (best-case + peak). Output: data to design control.
- **Stage 3 — design control law (data-grounded, user-reviewed).** Per-room =
  setpoint/preset only (#2, done). Central = PdC duty-cycle via BLOCCO (rest
  windows when in-envelope + comfort override + weather feed-forward). Load =
  anticipatory pre-cool + shading (#6).
- **Stage 4 — implement** in the integration: consume valve binary_sensors as
  demand; coalescing/duty controller actuating BLOCCO; envelope+pre-cool. Opt-in,
  guardrailed, tested, versioned.
- **Stage 5 — validate live + tune.**

## Architecture

- `const.py` — DOMAIN, PLATFORMS, call signals, `FANCOILS`, the verified `ZONES`
  map (zone → climate/fancoils/floor/EP device/emitter), `FAN_STAGES`.
- `coordinator.py` — `DataUpdateCoordinator` (30 s). Phase 0: read-only (fan
  speeds, cooling zones, consenso states).
- `__init__.py` — `async_setup_entry`/`async_unload_entry`; coordinator stored in
  `entry.runtime_data`; forwards to `PLATFORMS`.
- `sensor.py` — diagnostic `Cooling demand zones` (count of fancoils > 0).
- `config_flow.py` — single-instance, no fields.

Control will WRITE via `climate.set_preset_mode` (validated lever), never by
fighting KNX fan staging directly (until/unless the ETS question is resolved).

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
6. [~] #9 Demand coalescing → reborn as **central cooling duty-cycle via the
       Consenso BLOCCO (2/2/213)**, driven by real valve demand + weather. See
       "Summer cooling control plan" above. Stage 1 = expose valves+BLOCCO.
7. [x] ~~#3 Fan-stage modulation~~ DROPPED — water valve is on/off, fan is
       constant; no per-room cooling intensity to modulate (ETS-verified).
8. [ ] #5/#6 Outdoor shutoff + solar shading (Ecowitt + sun + south/west labels)
9. [ ] #7 Anticipatory radiant heating (winter) — caldo consenso mechanism TBD
10. [ ] #8 Interactive weekend scenes (actionable notification)

## Guardrails / domain rules

- **Manual override wins**: if a thermostat is changed by hand, back off for a set time.
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

- #3: does HA hold a fancoil fan stage, or does KNX re-assert? PARTIAL ANSWER:
  there ARE per-fancoil `switch.fancoil_*_manuale` switches (ON = HA holds the
  fan, KNX won't re-assert) — used by the camere-silenziose logic. Revisit #3
  with this lever instead of assuming an ETS change is required.
- Heating (`caldo`) consenso mechanism — radiant zone valves, not fan>0. Verify.
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
  (works on 3.10–3.13); PEP 695 `type` alias also fine on HA's 3.13.

## Background / planning docs

In the parent folder (`Home Assistant/`), authored during design:
- `hvac-restructure-status.html` — system status, gaps, To-Be user stories
- `hvac-implementation-plan.html` — per-story implementation plan, building blocks,
  build order, open spikes

These were built with the HA MCP connector (live introspection). Live HA control
during dev can stay in that Cowork session; code + git + deploy live here in
Claude Code.
