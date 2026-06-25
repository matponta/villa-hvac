# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. The durable source of truth is [`CLAUDE.md`](./CLAUDE.md);
this file is just the resume pointer + the live (non-repo) state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First, read CLAUDE.md in full — it's the verified source of truth (zone map, the
EV-FAN-valve cooling chain, the 5-stage valve-based summer plan). Also skim
../hvac-implementation-plan.html "Stato al 25 giu 2026" for the build status.

STATE (as of 2026-06-25):
- Custom component (NOT native automations). Repo github.com/matponta/villa-hvac,
  HACS, CI green on HA 2026.x, released v0.8.1. gh is authed via macOS keyring →
  push/release are tokenless. Dev: source .venv && ruff check + pytest (39 tests).
- DONE: #10 (per-zone enable switch), #1 (fused temp, thermostat-primary), #2 full
  (house-mode select + season-aware setpoint push + camere silenziose 2 bedrooms +
  away auto-escalation + options flow), #4 mechanism (window pause; only 3 vasistas
  wired). #3 DROPPED. #9 = central PdC duty-cycle via Consenso BLOCCO 2/2/213.
- Stage 1 DONE: EV FAN valve states (binary_sensor.fancoil_*_valvola, KNX 4/7/x) +
  switch.ct_blocco_freddo_villa are now live in HA; mapped in const.py as
  COOL_VALVES + CONSENSO_BLOCCO.
- NOT deployed to the real HA yet (it runs old v0.1.0; legacy clima_*/notte_*
  automations still drive the house). Deploy + retire legacy "when mature".

KEY FACTS (don't re-derive):
- Real per-room cooling demand = the EV FAN water valve (on/off), NOT fan% (fan is
  constant 100% in AUTO; valve cycles). Consenso freddo ≈ OR of valves.
- KNX thermostat temp sensor is laggy; EP reflects real air → EP is better for
  dynamics (revisit EP-primary). preset and setpoint are INDEPENDENT on the climate.
- Hard rooms are mass-bound (~0.85 C/h best-case) AND gain-limited at peak (camera
  padronale peak test: ~0 net cooling, held 27.1 C at 34 C outdoor). Levers for
  them = solar shading (#6) + anticipatory pre-cool (#7), NOT fan/coalescing tricks.
- Scheduled Claude tasks run headless WITHOUT the ha_* connector → never rely on
  them for live HA ops; use native HA automations for timed live actions.

NEXT (Stage 2): once ~1 day of valve history has accrued (it started ~2026-06-24
20:00 UTC), use the HA connector to analyze the REAL signals
(binary_sensor.fancoil_*_valvola): per-room cooling duty cycle, simultaneity/
staggering, valve<->consenso correlation, short-cycling — to decide whether a central
BLOCCO duty-cycle is warranted. Verify the BLOCCO polarity (block vs enable) in a
controlled, supervised step before any automation uses it. Then Stage 3: design the
control law (per-room setpoint = #2 done; central = BLOCCO duty-cycle; load = #6/#7).
Ask me before writing to the live HVAC.
```
