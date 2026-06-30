# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts + per-feature status) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build
checklist) · `../hvac-implementation-plan.html` (backbone). This file is the resume
pointer + the live (non-repo) state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified facts: zone map, EV-FAN-valve cooling chain,
BLOCCO polarity on=block, supervisor architecture, the #3-v2 / F2 / F3 / F4 notes).
Don't re-derive verified facts. MASTER_PLAN.md = build checklist.

ARCHITECTURE ("single organism"): one SupervisorEngine (engine.py) ticks off the
coordinator (30s), builds a unified HouseState, runs PURE policies (policies.py) +
stateful CONTROLLERS, and writes each lever ONCE via the pure manual-override-robust
arbiter (supervisor.py: reconcile/merge_desired). Pure core in supervisor.py imports
NO homeassistant/const. Engine merges CONTROLLERS-FIRST so a controller's setpoint
beats house_mode (it yields on disabled/paused/free-cool). Strict deploy-dark:
nothing actuates until switch.supervisor on; per-feature opt-in switches/options.
Fail-safe (async_fail_safe) releases BLOCCO + all fancoil manuale (fans->AUTO).

DONE (repo github.com/matponta/villa-hvac; gh authed via macOS keyring, tokenless
push/release; CI green; 186 tests; Python 3.14 / HA 2026.4.3):
- F1 (v0.17.0): #3 v2 comfort-band control + capacity-matched fan. KNX thermostat
  band is too narrow -> valve chatter; we impose a WIDE settable hysteresis by
  slamming the setpoint (RUN center-A valve open + capacity fan; REST center+A valve
  closed + fan_min; flip center+-B/2). Salotto+Cucina = one unit. Opt-in switch.fan_pacing.
- F2a (v0.18.0)/F2b (v0.19.0): online self-refining per-room model
  dT/dt=a(T_out-T)+b*S+c-k*u. ThermalEstimator OBSERVER (never actuates, ticks every
  cycle incl. deploy-dark) learns {a,b,c} on w=False windows, k on held-steady-fan
  windows; RoomModelStore persists; blended (prior->learned by confidence) model feeds
  the fan sizing. sensor.hvac_model_<zone> diagnostic. Opt-in OPT_MODEL_ENABLED (on).
- F3a (v0.20.0): regime peak/medium/low on sensor.hvac_plan (g_house/k_house/
  load_ratio), diagnostic-only; ratio trusted only for converged-k zones.
- F3b (v0.21.0): 12h per-room forward sim + grid-scan precool -> sensor.hvac_plan
  .room_plans (downsampled, recorder-excluded). PLAN-ONLY.
- F4a (v0.22.0): solar forecast (sun elev x clear-sky x cloud) -> replaces flat-solar
  prior. Opt-in OPT_SOLAR_FORECAST (off, validate vs gw3000a first).
- F4b (v0.23.0): per-room/per-fascia comfort windows -> band center relaxed outside
  the window (capped at duty_comfort_max, never a BP slam). Opt-in OPT_COMFORT_ENABLED.
- F3c (v0.24.0): demand COALESCING. RegimeCoordinator (engine-driven) -> in MEDIUM
  syncs all leaders RUN/REST via phase_override into FanBandController; REST via
  setpoint not BLOCCO; min-on/off 10/10; coordinator BLOCCO merged before
  DutyController (yields -> duty survives). Opt-in OPT_REGIME_ENABLED (off) AND duty
  AND fan_pacing. F4c MPC-lite = DEFERRED (owner: do heuristic first).

KEY FACTS (don't re-derive):
- Real per-room cooling demand = EV-FAN valve (binary_sensor.fancoil_*_valvola), NOT
  fan%. consenso_freddo = OR(valves). BLOCCO on=block (verified live 2026-06-30).
- Hard rooms gain-limited at 34C peak (~0 net cooling). The 4-param model can't
  reproduce that -> hard-room trajectories are ADVISORY until k learns; comfort is
  ALWAYS guaranteed by the live band, never the prediction.
- Identifiability: k learns only on held-fan windows, {a,b,c} only on no-cooling
  windows. Regime ratio / coalescing meaningful only once k converged.

LIVE STATE (NOT in repo): the real HA still runs the OLD v0.16.0 (pre-F1). On it:
switch.supervisor ON (base organism #2/#4/#5/#6/#10 live), switch.duty_cycle +
switch.fan_pacing ON (the OLD two-phase pacing -> this is what chattered salotto/
cucina off->100% at lunch), 9 legacy clima_*/notte_* automations DISABLED. v0.17-0.24
are released to GitHub but NOT yet pulled to the live HA. Dashboard "CoolClima"
(/cool-clima) + old "Clima" both exist. Scheduled Claude tasks have NO ha_* connector
(use the connector here for read-only; ASK before any live HVAC write).

DEV: source .venv/bin/activate; ruff check custom_components/villa_hvac tests &&
pytest -q (186). Per increment: small commit + tag + gh release on main.

NEXT (recommended order):
1) DEPLOY v0.24.0 to the live HA (HACS update villa_hvac -> 0.24.0 -> restart). This
   REPLACES the old chattering pacing with F1 band control AND starts the F2 model
   learning. On restart fan_pacing is persisted ON -> band control activates: the
   thermostat setpoints will visibly swing +-0.75C (the new wide hysteresis). regime/
   solar/comfort stay OFF (opt-in). VERIFY: salotto/cucina cycle long+uniform (not
   off->100 every 2min); watch sensor.hvac_model_* k_confidence climb over days.
2) After k converges (days/weeks of summer data): enable OPT_SOLAR_FORECAST (validate
   vs gw3000a on a clear day), then OPT_COMFORT_ENABLED (set the day/night fasce),
   then OPT_REGIME_ENABLED (coalescing) — one at a time, tune peak_ratio /
   precool_max_depth / band on real data.
3) Optional: refresh CoolClima with a "Brain" tab (sensor.hvac_model_* + regime +
   room_plans). Consider F4c MPC-lite only after the model is validated live.
4) Live-verify gates still open (supervised): held-low-fan smoothness (#3), mild-
   weather duty/coalescing tuning, winter caldo mechanism (#7, heating season).
5) When confident: delete the disabled legacy automations + tag v1.0.0.
ASK me before any write to the live HVAC.
```
