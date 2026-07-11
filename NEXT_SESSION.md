# Next session — kickstart prompt (BACKLOG build)

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts + per-feature status) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build
checklist) · [`STORY_SEFF.md`](./STORY_SEFF.md) (S_eff spec, shipped) ·
[`STORY_SPLIT_TRIO.md`](./STORY_SPLIT_TRIO.md) (split trio, shipped) ·
[`STORY_TIER1_COOLING_CONTROLLER.md`](./STORY_TIER1_COOLING_CONTROLLER.md)
(P3/P5/P6 remaining) · `../hvac-implementation-plan.html` (backbone). This file is
the resume pointer + live state.

```
Resume work on the villa_hvac Home Assistant custom integration — BACKLOG session.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified facts: zone map, EV-FAN-valve cooling chain,
BLOCCO polarity on=block, supervisor architecture, F1/F2/F3/F4 notes). Don't
re-derive verified facts. MASTER_PLAN.md = build checklist.

STATE (2026-07-11): repo == LIVE == v0.53.0 (1466 tests, ruff clean).
⚡ THE SUPERVISOR IS LIVE: switch.supervisor + auto_setback + vmc_auto ON since
2026-07-10 evening — the engine ACTUATES the villa. This is NO LONGER a
deploy-dark codebase: any change to the #2/#2b/#4/free-air/VMC paths changes
LIVE behavior the household relies on nightly. Small increments, pre-tag
adversarial review, never weaken the fail-safe invariants. Deploys (HACS +
restart) now restart a live-actuating supervisor — safe by design (fail-safe on
unload, switches restore), but verify after: supervisor back ON, hvac_levers=0,
no ERROR logs.

FIRST NIGHT (7/10→11) verified clean from the recorder: mode bridge propagated
Chiudi-notte in 11 ms; #2b silence latched same-second; heat-guard fired 00:17
(3-min debounce); auto-wake released at exactly 08:00:00; hvac_levers=0 all
night; zero errors; VMC2 bedroom unit stayed silent (night-quiet veto held),
VMC1 flushed once 00:40–00:50. Still OFF (owner's pace): fan_pacing, duty,
pv_bias, free_cooling, free_air, split_ac, unified_planner, regime, seff.

HA-SIDE STATE (not in this repo — applied live 2026-07-10 ~23:20):
- automation.clima_bridge_modalita_casa_supervisor_one_way: ONE-WAY bridge
  input_select.modalita_casa → select.house_mode (options match by design).
  The physical buttons keep working; select.house_mode is the climate truth.
- Legacy climate automations DISABLED, not deleted (rollback = re-enable):
  clima_applica_modalita_casa, clima_rientro_in_casa_ripristina_fancoil,
  notte_guardia_caldo_camera_* (×3), notte_sveglia_automatica_camere,
  clima_risincronizza_modalita_all_avvio (PROVEN hazard: automation.trigger
  executes disabled automations), clima_master_temperatura_casa.
  Verify clima_backup_via_quando_esco too.
- Smarty VMC switch (switch.10_5_150_27_boost) blips unavailable every
  ~10–25 min — flaky integration; the edge-triggered VmcController is immune.

BACKLOG (priority order — pick from the top unless the owner redirects):

1. #2b HEAT-GUARD CHILLED WATER [RECOMMENDED FIRST: small, owner-asked,
   evidence in hand]. Today the guard only spins the fan (33%) above 26 °C;
   the valve stays with the KNX thermostat at the Notte setpoint 27 (24+3), so
   the 26–27 band is warm-air circulation with the valve CLOSED. First-night
   evidence: padronale crossed 26.0 at 00:04 and climbed to 26.8 by 02:40 —
   whole night in the dead-band, no chilled water. Fix: guard-active → ALSO
   nudge that room's setpoint down (e.g. threshold−0.5 or reuse the band slam)
   so the valve opens; release with the guard's 10-min-below hysteresis, the
   08:00 auto-wake, AND async_fail_safe. Design constraints: the guard's
   temperature-lever opinion must outrank house_mode #2a in the arbiter merge
   (same precedence mechanism the band uses — NightSilenceController is already
   a merge controller, C1); never fight camere-silenziose's own manuale hold;
   this path is LIVE — golden-test current behavior first, then change.

2. PER-ROOM OCCUPANCY ROSTER (#2 evolution): every zone has ep_occ mapped in
   ZONES but NOTHING consumes it. Goal: a vacant room relaxes toward its own
   setback (the Gabriele case: empty bedroom on comfort schedule all day).
   EP occupancy is flappy → debounce/latch like presence. Design first (small
   story doc): per-zone opt-in? offset-based relax vs preset? interaction with
   #2b bedrooms + comfort windows (F4b).

3. #2 OFFSET INTO plan_center_schedule: the per-room offset (v0.49.0) is
   applied at resolve_center/house_mode/precool but NOT in the unified-planner
   schedule base — a HARD GATE before switch.unified_planner can ever be
   enabled. Mechanical, well-scoped.

4. FREE_AIR → PER-ROOM "OPEN WINDOWS" (owner ask): rename switch.free_air →
   "Open windows"; one switch per cooled zone (pausing just that zone);
   window CONTACT SENSORS to be installed on the cooled rooms later slot into
   the existing #4 `window` key/WindowController (manual switch = fallback for
   sensor-less rooms). The switch layer can ship BEFORE the sensors exist.

5. LEGACY CLEANUP → v1.0.0: after ~1 week of clean supervisor nights, DELETE
   the disabled automations + the buonanotte/sveglia scripts' climate branches
   + automation.sistema_ricrea_group_presenza_adulti_all_avvio (obsolete since
   v0.46.0 watches person.* directly). Consider rewiring the physical buttons
   to select.house_mode directly and retiring the bridge last.

6. TIER-1 TRAIN (STORY_TIER1): P3 delete-trio is STOP-gated on the live soak
   of the merged CoolingController — the soak STARTED 2026-07-10 when the
   supervisor went live; give it 1–2 clean weeks (watch hvac_levers + nightly
   behavior), then P3 → P5 (R2 deviation-space) → P6 (R3 REST-quorum + boot
   manuale sweep). P4 feature_graph already shipped (v0.47.0).

7. OUTSIDE-AIR MERGE (free-cooling × open-windows, owner ask): deliberately
   UNDESIGNED until weeks of live data show how the two intertwine.
   Candidates noted in CLAUDE.md #5.

Smaller follow-ups: VMC thresholds (const → options flow) · #6
cycles_since_restart resets on restart (total is restored — cosmetic) ·
night-guard threshold tuning on real nights (26.0 default).

LIVE-OPS for the owner (not build work — support if asked): watch S_eff
geometry on the Modello tab → flip seff_enabled → STORY_SEFF §8 gates ~1wk;
fan_pacing daytime trial; free_cooling opt-in when wanted; split_ac AFTER
disabling automation.circolazione_aria_cantina_vini.

RULES unchanged: pytest + ruff green on the pinned target
(pytest-homeassistant-custom-component==0.13.324 = HA 2026.4.3 / Py 3.14);
small commit + tag + gh release per increment; pre-tag adversarial review
(workflow if budget allows, else inline 3-lens + refutation); fail-safe
invariants byte-preserved; HA connector for live diagnosis (read-only unless
the owner asks); owner-visible behavior changes get a household-manual update
(artifact 8d1ef72b + PDF in repo root). Known quirks: ~40s KNX unavailable
blips (nightly ~03:00 backup) — ignore singles; the ThermalEstimator gap guard
covers the long ones.
```
