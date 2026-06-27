# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build checklist) ·
`../hvac-implementation-plan.html` (the backbone). This file is the resume pointer
+ the live (non-repo) state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified source of truth: zone map, EV-FAN-valve
cooling chain, supervisor architecture). MASTER_PLAN.md = build checklist;
../hvac-implementation-plan.html = the backbone. Don't re-derive verified facts.

ARCHITECTURE (the "single organism"): one SupervisorEngine (engine.py) ticks off
the coordinator (30s), builds a unified HouseState, runs a priority POLICY STACK,
and writes each lever ONCE via a pure manual-override-robust arbiter
(supervisor.py: reconcile + merge_desired). Policies are pure (policies.py).
Strict deploy-dark: nothing actuates until switch.supervisor is on.

DONE this build (repo github.com/matponta/villa-hvac; gh authed via macOS keyring,
tokenless push/release; released through v0.14.1; CI green; 114 tests):
- Phase 0: tests pinned to the DEPLOY TARGET — HA 2026.4.3 / Python 3.14
  (the 2026.4.x line dropped 3.13). venv is python@3.14 (brew).
- A (v0.9.0): supervisor backbone + write-arbiter (write-confirm/re-assert
  manual-override) + fail-safe (release BLOCCO on unload) + cutover of #2/#4/#10
  onto the engine. #1 fused temp + #2 house-mode/camere/away + #4 window + #10
  disable all run AS POLICIES now (the old controllers are triggers).
- B (v0.10.0): #5 free_cool_policy (summer + gw3000a_outdoor < OPT_FREE_COOL_OUTDOOR
  -> fancoils to building_protection).
- C (v0.11.0): #6 shading_policy + runtime cover resolver (cover -> device area_id/
  labels -> orientation/floor; skip orphans). New cover lever (close_cover).
- D (v0.12.0/v0.13.0/v0.14.0/v0.14.1): #9 central duty-cycle via Consenso BLOCCO
  (DutyController: cap stint OPT_DUTY_MAX_STINT -> cooloff OPT_DUTY_COOLOFF;
  comfort-max abort) + switch.duty_cycle; duty-adaptive peak-skip (OPT_DUTY_PEAK_
  OUTDOOR); #3 fan pacing (FanPacingController two-phase, switch.fan_pacing);
  12h FORECAST PLANNER (plan_run on weather.forecast_home, re-fetched every 30min,
  margin gate -> precool: suppress cooloff + nudge setpoints colder).

NOT DEPLOYED to the real HA yet (it still runs the legacy clima_*/notte_*
automations on the old v0.1.0). Deploy-dark: copy custom_components/villa_hvac to
/config, restart, then flip switch.supervisor to light it up; per-feature
switches: duty_cycle, fan_pacing (default off). Retire legacy at deploy.

KEY FACTS (don't re-derive):
- Real per-room cooling demand = EV FAN water valve on/off (binary_sensor.
  fancoil_*_valvola), NOT fan% (fan ~constant 100% in AUTO; in MANUAL it HOLDS the
  set %, KNX doesn't re-assert — that's what #3 pacing uses). consenso = OR(valves).
- Hard rooms gain-limited at peak (~0 net cooling at 34C outdoor) -> levers are
  load reduction (#6 shading) + pre-cool (#9 planner / #7), not fan/coalescing.
- Stage 2 (50h heatwave): no compressor short-cycling; demand coincident; so #9
  coalescing only helps in MILD weather -> duty-adaptive, tune on post-deploy data.
- Scheduled Claude tasks run headless WITHOUT the ha_* connector. Use the connector
  here for read-only analysis; ASK before any live HVAC WRITE.

DEV: source .venv/bin/activate; ruff check custom_components/villa_hvac tests &&
pytest -q (114 tests). Per increment: small commit + tag + gh release on main.

NEXT — remaining roadmap (all still deploy-dark / code+tests):
- #7 winter radiant pre-heat (caldo mechanism unverified -> behind a flag;
  summer pre-cool already covered by the #9 planner).
- #8 interactive weekend scenes (actionable notify).
- #11 next-12h heating/cooling PLAN visualization (expose RunPlan/DutyState/
  precool as a sensor + dashboard timeline card).
- DEPLOY (Phase G) + LIVE-VERIFY GATES (supervised, never headless):
  (1) BLOCCO polarity (one toggle, watch consenso drop ~1-2min);
  (2) held-low-fan cooling test (does a held low fan cool smoothly / stop the
      valve bang-banging? tunes FAN_PACING_*);
  (3) mild-weather valve history (tunes OPT_DUTY_* thresholds);
  (4) winter caldo mechanism (for #7).
Ask me before writing to the live HVAC.
```
