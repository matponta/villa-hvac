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
| F2 | #11 plan visualization — next-12h heating/cooling plan (forecast peak + pre-cool + duty run/rest + per-zone setpoints/shading) as a sensor + dashboard timeline card; builds on #9 RunPlan/DutyState/precool | — |
| G | Deploy + retire legacy + verify BLOCCO polarity + tune #9 on mild data | v1.0.0 |

Cadence: build A→D back-to-back, then check in before #3-rest/#7/#8 + deploy.

## Live-verify gates (supervised, at deploy — never headless)

BLOCCO polarity · held-low-fan% cooling/valve test (#3) · mild-weather valve
history (#9 tuning) · winter `caldo` mechanism (#7). (#6 cover/orientation map is
runtime registry-resolved + verified — no longer a gate; just confirm all relevant
covers carry an orientation label.)
