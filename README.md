# Villa HVAC

Custom Home Assistant integration that orchestrates the KNX climate system of
Villa Pontacolone: occupancy-based setback, window pause, fancoil fan logic,
solar shading, demand coalescing and long-term zone disable — driven by the
Everything Presence One sensors, the Ecowitt weather station and the S5A
condominial heat-pump signals.

> **Status: 0.5.0 — full house-mode setback, quiet nights, away escalation.**
> Cooling-demand sensor, per-zone fused temperature (#1), per-zone enable
> switch (#10), and the complete #2: house-mode presets (#2a), camere silenziose
> (#2b), and away auto-escalation (#2c).

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
- [x] #10 Long-term zone disable (`switch` per zone: off → `building_protection`)
- [x] #1 Fused zone temperature (`sensor` per zone, thermostat-primary)¹
- [ ] _circle back_: EP-primary temperature with time-varying offset calibration
- [x] #2 Occupancy / night setback — house-mode `select` (Casa/Via/Notte/Vacanza)
  drives KNX presets + global `Auto setback` switch; Notte runs *camere silenziose*
  on the 2 bedrooms (manuale + fan off, heat-guard, auto-wake); long absence
  auto-escalates Casa/Notte→Via and restores Casa on return. Tunables in options.
  _(Cleanup pending: remove the now-replaced HA automations/scripts.)_
- [ ] #4 Window-open pause (bidirectional)
- [ ] #3 Fan-stage modulation (pending ETS spike)
- [ ] #9 PdC demand coalescing
- [ ] #5/#6 Outdoor shutoff + solar shading
- [ ] #7 Anticipatory radiant heating (winter)
- [ ] #8 Interactive weekend scenes

> ¹ #1 is **thermostat-primary**: the fused temp reads each zone's clean
> `sensor.clima_*` twin and falls back to the climate `current_temperature`
> (sources older than 30 min are treated as stale). EP sensors were measured to
> be ~5 °C biased with a time-of-day-dependent drift, so they are reserved for
> occupancy (#2); see `EP_TEMP_OFFSETS` in `const.py` for the recorded data and
> the planned EP-primary revisit.

## Install (HACS custom repository)

1. Push this repo to a **public** GitHub repository.
2. In Home Assistant: HACS → ⋮ → *Custom repositories* → add the repo URL,
   category **Integration**.
3. Install **Villa HVAC**, then **restart Home Assistant**.
4. Settings → Devices & Services → *Add Integration* → **Villa HVAC**.

Update `documentation`/`issue_tracker` in `manifest.json` and the badges/URLs
here with the real GitHub path (replace `CHANGEME`), and `codeowners`.

## Dev / deploy loop

Develop in **Claude Code** (runs locally with your git/GitHub auth and can deploy
to `/config`). See [`CLAUDE.md`](./CLAUDE.md) for full project context.

Deployment to the live HA is manual:
- **Fast loop:** sync `custom_components/villa_hvac/` to `/config/custom_components/`
  (Samba / Studio Code Server App / `git pull`) and restart HA.
- **Release loop:** tag a GitHub **release**; HACS picks up the new version.

### First-time git push (run locally)

```bash
cd "<...>/Documents/Claude/Projects/Home Assistant/villa-hvac"
rm -f .git/index.lock          # clear the stale lock from the sandbox init
git remote add origin git@github.com:matponta/villa-hvac.git
git push -u origin main
# or, with the GitHub CLI:
# gh repo create villa-hvac --public --source=. --push
```

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
   ├─ coordinator.py    # polls call signals + fancoil demand + fused zone temps
   ├─ temperature.py    # pure temperature-fusion logic (#1)
   ├─ controller.py     # house-mode → KNX preset driver (#2a)
   ├─ night.py          # camere silenziose: bedroom silence + heat-guard (#2b)
   ├─ away.py           # away auto-escalation: presence → Via/Casa (#2c)
   ├─ sensor.py         # cooling demand zones + per-zone fused temperature (#1)
   ├─ select.py         # house-mode select: Casa/Via/Notte/Vacanza (#2a)
   ├─ config_flow.py    # single-instance setup + options (night threshold/wake)
   └─ switch.py         # per-zone enable (#10) + global Auto setback (#2a)
```
