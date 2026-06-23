# Villa HVAC

Custom Home Assistant integration that orchestrates the KNX climate system of
Villa Pontacolone: occupancy-based setback, window pause, fancoil fan logic,
solar shading, demand coalescing and long-term zone disable — driven by the
Everything Presence One sensors, the Ecowitt weather station and the S5A
condominial heat-pump signals.

> **Status: 0.1.0 — Phase 0 skeleton (read-only).**
> Exposes one diagnostic sensor; control behaviors are added incrementally.

## Why a custom component (and the caveat)

The orchestration could also live in native packages + blueprints. We chose a
custom component for clean code, testing and git versioning — accepting the
higher maintenance cost of owning a real HA integration.

## Key validated facts (2026-06-23)

- The **real PdC call is not `climate.hvac_action`** (that's just the mode).
  It is the KNX `binary_sensor.ct_consenso_freddo_villa` (cooling) /
  `binary_sensor.ct_consenso_caldo_villa` (heating).
- **Cooling consenso turns on when any fancoil fan > 0.**
- **Lever (no ETS needed):** setting a KNX thermostat preset to
  `building_protection` drives its fancoil fan to 0 → the cooling consenso
  drops off (after a ~1–2 min KNX off-delay). Verified house-wide.
- Fancoils are **3-speed** (33/67/100). Continuous modulation (smooth fan)
  may require an ETS change — still open.

## Roadmap

- [x] 0.1 Phase 0: read-only KPI sensor (`Cooling demand zones`)
- [ ] #10 Long-term zone disable (`switch` per zone)
- [ ] #1 Fused zone temperature (`sensor` per zone: EP + KNX fallback)
- [ ] #2 Occupancy / night setback (preset lever)
- [ ] #4 Window-open pause (bidirectional)
- [ ] #3 Fan-stage modulation (pending ETS spike)
- [ ] #9 PdC demand coalescing
- [ ] #5/#6 Outdoor shutoff + solar shading
- [ ] #7 Anticipatory radiant heating (winter)
- [ ] #8 Interactive weekend scenes

## Install (HACS custom repository)

1. Push this repo to a **public** GitHub repository.
2. In Home Assistant: HACS → ⋮ → *Custom repositories* → add the repo URL,
   category **Integration**.
3. Install **Villa HVAC**, then **restart Home Assistant**.
4. Settings → Devices & Services → *Add Integration* → **Villa HVAC**.

Update `documentation`/`issue_tracker` in `manifest.json` and the badges/URLs
here with the real GitHub path (replace `CHANGEME`), and `codeowners`.

## Dev / deploy loop

Claude authors the code in this repo; deployment to the live HA is manual:
- **Fast loop:** sync `custom_components/villa_hvac/` to `/config/custom_components/`
  (Samba / Studio Code Server App / `git pull`) and restart HA.
- **Release loop:** tag a GitHub **release**; HACS picks up the new version.

## Layout

```
villa-hvac/
├─ hacs.json
├─ README.md
└─ custom_components/villa_hvac/
   ├─ manifest.json
   ├─ const.py          # zone map + call signals (verified)
   ├─ __init__.py       # setup/unload, coordinator in runtime_data
   ├─ config_flow.py    # single-instance UI setup
   ├─ coordinator.py    # polls call signals + fancoil demand
   └─ sensor.py         # diagnostic: cooling demand zones
```
