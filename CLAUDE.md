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
2. [ ] #10 Long-term zone disable — `switch` per zone (`input_boolean`-like);
       off → force `building_protection`, keep frost protection
3. [ ] #1 Fused zone temperature — `sensor` per zone: EP (offset-calibrated) with
       fallback to the KNX thermostat sensor when EP is unavailable
4. [ ] #2 Occupancy / night setback — preset lever, guardrail anti short-cycling
5. [ ] #4 Window pause (bidirectional) — vasistas/contacts OR EP↔KNX divergence
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

- #3: does HA hold a fancoil fan stage, or does KNX re-assert? (likely ETS needed)
- Heating (`caldo`) consenso mechanism — radiant zone valves, not fan>0. Verify.
- Per-zone EP temperature offset calibration values.
- Per-zone EP temp/occupancy entity_ids to complete in `ZONES`.

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
