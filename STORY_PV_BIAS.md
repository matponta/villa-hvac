# PV/energy-aware daily pre-cool (F4c-lite) — build spec

Reframed with the owner 2026-07-01. Supersedes the naive "cool when battery
charging" sketch. Objective: **bank coolth at the thermodynamically most effective
hours, using the solar forecast + battery as a buffer, so the hot/expensive evening
needs minimal compressor** — pure self-consumption, comfort a HARD bound.

## Owner's framing (the objective)
- The battery already time-shifts solar → "charging" is a weak trigger.
- Pre-cool WHEN it's most effective thermodynamically: cool hours (low T_out + low
  solar gain) bank real coolth per kWh; the 34°C peak nets ~0 (verified) → useless.
- Whole-day balance with the battery as buffer: if forecast daily solar ≥ 24h
  consumption → bank free solar coolth in the efficient+sunny hours. If solar <
  consumption (hot day, will import anyway) → still pre-cool in the cheap EFFICIENT
  morning hours (grid-draw OK), and let midday solar refill the battery to carry the
  evening home base load instead of wasting it on ineffective peak cooling.

## Signals (verified live 2026-07-01)
- PdC electrical load: `sensor.shellypro3em63_e08cfe9573ac_power` (W, local clamp =
  PdC + pumps, stable base). Better than `house_load_power_2`.
- Condominio battery SoC `sensor.battery_percentage_2` (%); signed power
  `sensor.energy_battery_..._2..._net_power` (W, NEG=charging); grid
  `sensor.energy_grid_..._2..._net_power` (W, POS=import). Node ne=199688300.
  (See memory [[condominio-pv-energy-map]] for the `_2` gotcha + spike/lag caveats.)
- PV production forecast: `sensor.fusion_solar_condominio_panel_production_remaining_today`
  (kWh remaining + 5-min curve attr) + Forecast.Solar `sensor.energy_*`.
- Temp forecast: OPT_WEATHER_ENTITY (OWM). Solar-gain forecast: the nowcast-anchored
  `solar_curve_v2` (F4a-v2). Per-room learned model {a,b,c,k} (F2).

## Pure core (supervisor.py — unit-tested, no HA imports)
- `cooling_effectiveness(t_room, t_out, solar, *, a,b,c,k) -> float`
  = `k − cooling_load(...)` = net °C/h of cooling at full fan. ≤0 at the peak.
- `energy_precool_decision(...) -> EnergyPrecoolDecision{mode, floor, ...}`:
  three-way per-cycle decision over an hourly effectiveness horizon:
  - `eff_now = effectiveness[now]`, `eff_peak = max` over the horizon.
  - `eff_peak ≤ eff_min` → **HOLD** (nothing effective ahead; normal band).
  - `solar_rich = pv_kwh_remaining ≥ consumption_kwh_remaining` (daily balance).
  - `eff_now ≥ eff_fraction·eff_peak` (now is among the efficient hours) → **BANK**
    to `floor_rich` (solar-rich) else `floor_poor` (gentler, limit grid draw).
  - `eff_now ≤ eff_min` (actively inefficient hot hour) → **COAST** (defer within
    comfort).
  - else → **HOLD**.
  Battery is a state input (SoC/charging) that tunes aggressiveness, NOT the trigger.

## Execution (reuse existing levers — no new lever)
- **BANK** → band center = `max(floor, center_base − pv_precool_offset)`; suppress the
  duty cooloff (keep cooling while it's the good hour). Like the existing precool path.
- **COAST** → band center = `min(center_base + coast_relax, duty_comfort_max)` (defer,
  like F4b comfort_relax). The band still cools if a room breaches — comfort wins.
- **HOLD** → no opinion.
- Comfort is a HARD bound both ways: never below `floor` (~22, owner choice), never
  above `duty_comfort_max`. Fail-safe already covers it (reuses setpoint/duty).

## Anti-thrash / robustness
- Smooth + spike-clip the noisy FusionSolar W signals (EMA in the stateful controller).
- The bank/coast decision keys off the HOURLY effectiveness ranking (stable), not the
  instantaneous W, so it won't flip on cloud transients; add a min-dwell.
- Configurable entity ids (the `_2` naming is fragile).

## Entities / config
- Opt-in `switch.pv_bias` (deploy-dark). Options: floor_rich (22), floor_poor (23),
  pv_precool_offset, coast_relax, eff_fraction (0.6), eff_min, daily_need_kwh
  (consumption estimate, tunable — refine from Shelly history later), + the entity ids.
- Diagnostic `sensor.hvac_energy` (or extend hvac_plan): mode (bank/coast/hold),
  eff_now/eff_peak, solar_rich, pv_kwh_remaining, battery SoC, smoothed grid/battery.

## Build phases
1. Pure core + tests (this increment).  2. Engine energy-context + stateful
controller + band/duty wiring.  3. Opt-in switch + options + diagnostic sensor.
4. Adversarial review + live-validate signals, then enable.

Full LP/MPC (EMHASS-style) remains a later option; this heuristic captures ~80%.

## Adversarial review — fixes applied (v0.28.0, 24-agent review)
- **UTC→local day clock:** `frac_remaining` now uses `dt_util.now()` (was `state.now`
  = UTC → ~2h skew on the solar-rich/poor floor choice).
- **Effectiveness at the true center:** ranked at `house_setpoint + mode_offset`, not
  the bare setpoint (mode_offset understated net cooling in Via/Notte).
- **Requires a real solar forecast:** yields when the solar curve is `flat`
  (OPT_SOLAR_FORECAST off) — a flat curve collapses the peak-defer ranking.
- **Degenerate solar-rich guard:** `consumption_kwh_remaining > 0` (end-of-day
  0 ≥ 0 no longer flips solar-rich → deepest floor at night on grid).
- **COAST respects comfort windows:** in-window (comfort enabled + relax 0) it does
  NOT defer; out-of-window defers by `max(pv_coast_relax, comfort_relax)`.
- **Min-dwell (`PV_BIAS_MIN_DWELL` 20 min):** holds the bank/coast/hold decision to
  stop mode flips slamming the valve (a flip jumps the center ~2°C >> band).
- **Best-effort:** `_pv_bias_apply` wrapped in try/except — never breaks the cycle.

## Known caveats (documented, not blocking)
- **Comfort cap is on the CENTER:** COAST caps at `duty_comfort_max` and BANK at the
  floor, but band_step adds ±B/2, so the peak/trough is ~cap±0.75. Consistent with
  F4b. Not an inviolable hard peak — size `duty_comfort_max`/floors accordingly.
- **Do NOT co-enable with regime coalescing (OPT_REGIME_ENABLED) yet:** the regime
  coordinator decides RUN/REST off the BASE center while the band applies the
  PV-shifted center → unverified composition. Enable one at a time.
