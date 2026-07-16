# Villa HVAC

Custom Home Assistant integration that **supervises** the KNX climate system of
Villa Pontacolone. It does not replace the room thermostats — it reads the room
sensors, presence, weather and the heat-pump call signals, and writes back KNX
presets, setpoints, fan speeds and the central cooling lever to coordinate the
whole villa: occupancy/night setback, window pause, fan pacing, solar shading,
demand coalescing, hardware protection and anticipatory pre-cool.

> **Status: v0.65.0 — LIVE.** The supervisor actuates the villa. Core setback +
> season-aware setpoints, quiet nights, away escalation, window contacts, free
> cooling + VMC night flush, solar shading, the split-AC trio, the rack + P1
> hardware guards and the living-room steady fan governor are all shipped;
> duty-cycle, PV/forecast pre-cool and the unified planner are built and staged
> behind opt-in switches.

Target: Home Assistant **2026.4.3** (Python ≥ 3.14). Single instance, config-flow.
Full engineering context lives in [`CLAUDE.md`](./CLAUDE.md).

## Design in one paragraph

Everything writes through **one engine** (`engine.py`): each 30 s tick it builds a
`HouseState`, runs a stack of **stateful controllers** (cooling/duty/pacing, night
silence, rack + P1 guards, splits) and **pure preset policies** (disabled zones >
window pause > free-cool > pre-cool > house mode, plus a separate solar-shading
cover policy), merges their opinions one lever
at a time through an idempotent, manual-override-robust arbiter (`reconcile`), and
applies the result. Nothing actuates until the master `switch.supervisor` is on
(**strict deploy-dark**), and `async_fail_safe` releases the central BLOCCO, hands
fans back to a live AUTO state and restores presets/setpoints on unload or crash.
The pure decision core lives in the HA-import-free `supervisor/` package and is
unit-tested in isolation (649 tests).

## Key verified facts

These were measured live; do not re-derive them (see `CLAUDE.md` for the full log):

- The real PdC call is **not** `climate.hvac_action` — it is the KNX
  `binary_sensor.ct_consenso_freddo_villa` (cooling) / `ct_consenso_caldo_villa`
  (heating).
- Per-room cooling **demand is the fancoil chilled-water valve** (EV FAN, on/off —
  `binary_sensor.fancoil_*_valvola`), **not** fan %. In AUTO the fan runs ~constant;
  the valve cycles to hold setpoint.
- **Levers:** a KNX preset of `building_protection` drives a zone off (consenso
  drops after a ~1–2 min off-delay); the central `switch.ct_blocco_freddo_villa`
  force-stops the villa cooling call (`on` = block, verified). In MANUAL
  (`switch.fancoil_*_manuale` on) a fancoil fan holds an exact % indefinitely — so
  fan % is a real per-room rate lever there.
- **A KNX fancoil in AUTO does not restart a fan whose switch object was written
  OFF**, and fan-OFF interlocks the valve CLOSED — so every path that turns a fan
  off must have a matching re-arm (fail-safe + a self-heal watchdog enforce this).
- Fan/room wiring has physical quirks (owner-verified): Salotto↔Kitchen fan/valve/
  manuale entities are swapped; Pianerottolo P1 owns no fan (the rack + office fans
  vent into it); the Sala Giochi fancoil is out of service.

## Features

| Area | Status |
|---|---|
| #1 Per-zone fused temperature (thermostat-primary, staleness-guarded) | ✅ |
| #2 House-mode select (Casa/Via/Notte/Vacanza) → presets + season-aware setpoints + per-room trim | ✅ |
| #2b Camere silenziose (persistent per-bedroom night-silence switches + heat-guard chilled water + auto-wake) | ✅ |
| #2c Away auto-escalation (presence → Via/Casa) | ✅ |
| #4 Window pause (3 vasistas + 6 Shelly BLU contacts; long-open alert) | ✅ |
| #10 Long-term per-zone disable | ✅ |
| #5 Free cooling (outdoor coast, opt-in) + VMC night flush (night-quiet gate) | ✅ |
| #6 Solar shading (per-room position, proportional) + split-AC trio (opt-in) | ✅ |
| #6 Rack hardware guard + P1 "both fans" secondary trigger (default on) | ✅ |
| #3 v3 Living-room steady fan governor (opt-in, shadow → actuate) | ✅ |
| #9 Duty-cycle coalescing via BLOCCO + forecast pre-cool | ✅ built, opt-in |
| #7 / PV bias / S_eff per-facade solar / F4c unified planner | ✅ built, staged behind opt-in gates |
| #8 Return-home pre-conditioning | ✅ built, opt-in |
| #11 12-hour plan visualization (`sensor.hvac_plan`) | ✅ |

Opt-in switches layer on top of the master; several stay deploy-dark until
per-room model convergence + live validation gates pass. See the switchboard in
the household manual and `MASTER_PLAN.md` for the build checklist.

## Install (HACS custom repository)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/matponta/villa-hvac`,
   category **Integration**.
2. Install **Villa HVAC**, then **restart Home Assistant**.
3. Settings → Devices & Services → *Add Integration* → **Villa HVAC**.
4. Flip `switch.supervisor` on to light up the migrated behaviors at once.

## Dev / deploy

- Code lives in this repo (git root). Develop in Claude Code; lint with `ruff`,
  test with `pytest` against `pytest-homeassistant-custom-component` pinned to the
  deploy target (HA 2026.4.3 / Py 3.14). CI runs on the target.
- **Release loop:** commit to `main`, tag `vX.Y.Z`, publish a GitHub release —
  HACS picks it up; update in HACS + restart HA.
- **Fast loop:** sync `custom_components/villa_hvac/` into `/config/custom_components/`
  (Samba / VS Code Server / `git pull`) and restart HA.

## Layout

```
villa-hvac/
├─ hacs.json · README.md · CLAUDE.md · MASTER_PLAN.md · STORY_*.md
└─ custom_components/villa_hvac/
   ├─ const.py            # zone map, call signals, valve/fan wiring, tunables
   ├─ __init__.py         # wires coordinator + engine (controllers + policies)
   ├─ coordinator.py      # 30 s read-only poll (fan %, valves, consenso, fused temps)
   ├─ engine.py           # SupervisorEngine: build state → controllers+policies → reconcile → apply; fail-safe
   ├─ supervisor/         # PURE core (no HA imports): arbiter · control_law · thermal · model · planner · solar · returnhome
   ├─ temperature.py      # #1 pure temperature-fusion helper (thermostat-primary)
   ├─ policies.py         # pure preset policies + CoolingController (folds duty/band/regime) + SplitGroupController + ThermalEstimator
   ├─ governor.py         # #3 v3 living-room steady fan governor
   ├─ rack.py             # RackGuardController + P1GuardController (hardware protection)
   ├─ night.py            # camere silenziose (#2b) merge controller
   ├─ controller.py · window.py · away.py · vmc.py   # #2a/#4/#2c/#5 triggers
   ├─ supervisor_config.py  # options → frozen per-cycle config snapshot
   ├─ sensor.py · select.py · number.py · date.py · switch.py · config_flow.py
   └─ translations/
```
