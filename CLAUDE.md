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
- **Cooling consenso turns ON when any fancoil fan > 0.** So a zone's cooling
  demand == its fancoil `percentage > 0`.
- **The lever (NO ETS needed):** setting a KNX thermostat preset to
  `building_protection` drives its fancoil fan to **0** → cooling consenso drops
  off after a **~1–2 min KNX off-delay**. Verified house-wide on all 6
  fancoil-driving thermostats. This is how #2/#9/#10 gate the call.
- Fancoils are **3-speed** (33/67/100 = low/med/high). HA `fan.set_percentage`
  is accepted but the KNX actuator **quantizes** it and likely **re-asserts** on
  its own control cycle → continuous smooth modulation (#3) probably needs an
  **ETS change** (fan from external value). STILL OPEN — verify before building #3.
- KNX climates: `hvac_modes: [cool, heat]` (no `off`), `supported_features: 17`
  (target temp + preset), presets `[building_protection, auto, economy, comfort,
  standby]`. Fan speed is a **separate `fan.*` entity**, not on the climate.

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
       cool/heat state (or forced via the `season` option). Defaults: summer
       Via +5 / Notte +3, winter Via +2 / Notte +4 (Casa +0, Vacanza none).
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
6. [ ] #9 Demand coalescing — batch single-zone calls (the ~1–2 min off-delay helps)
7. [ ] #3 Fan-stage modulation — BLOCKED on ETS spike
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
