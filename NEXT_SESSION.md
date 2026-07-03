# Next session — kickstart prompt

Paste the block below as the first message in a fresh Claude Code session to resume
this work with full context. Durable source of truth: [`CLAUDE.md`](./CLAUDE.md)
(verified facts + per-feature status) · [`MASTER_PLAN.md`](./MASTER_PLAN.md) (build
checklist) · `../hvac-implementation-plan.html` (backbone + **prioritized backlog,
updated 3 Jul from a live audit**). This file is the resume pointer + live state.

```
Resume work on the villa_hvac Home Assistant custom integration.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
First read CLAUDE.md in full (verified facts: zone map, EV-FAN-valve cooling chain,
BLOCCO polarity on=block, supervisor architecture, F1/F2/F3/F4 notes). Don't
re-derive verified facts. MASTER_PLAN.md = build checklist. Prioritized backlog
with live forensics: ../hvac-implementation-plan.html §Backlog.

STATE (2026-07-04, overnight session): repo = v0.41.0 (1343 tests, CI green);
LIVE = v0.40.0 until the owner deploys v0.41.0 (HACS update + restart —
recommended FIRST thing in the morning). v0.40.1 + v0.41.0 = the MORNING-DEFECT
TRAIN, all adversarially reviewed pre-tag (two 3-lens workflows + refutation):

1. FAN LEVER ON/OFF (v0.40.1) — read = delivered airflow (OFF→0, ON→%, else
   transient); write = turn_on(percentage) / turn_off + set_percentage(0) disarm
   (separate KNX switch 5/0/x + speed 5/4/x objects); state-aware fan_pct for
   the F2 learner + coordinator; RUN-with-fan-OFF watchdog (WARN once ~5 min).
   Kills the padronale dead-morning (fan off + retained % read "satisfied" while
   the KNX interlock held the EV valve closed).
2. NIGHT WAKE CLOCK-DERIVED (v0.41.0) — woken = latch OR clock in
   [wake_time, wake_time+12h); a reboot/reload in Notte after 08:00 no longer
   re-silences the bedrooms until the mode leaves Notte.
3. RUN FAN SIZING LAW (v0.41.0) — run_fan_pct = capacity-match of envelope gain
   + STORED-HEAT extraction (temp−center)/COOL_PULLDOWN_HOURS(2h): hot rooms
   saturate to 100% BY THE LAW (owner requirement, not a guardrail); RUN floor
   COOL_RUN_FAN_FLOOR 20% (fan 0% in RUN = valve closed by interlock); at-peak
   above-band 100% backstop behind a deadbanded peak_latch (enter ≥30, exit
   <29.5 — no hunting on outdoor jitter). ONE pure law shared by trio + fold +
   planner sim (parity pinned by tests).
4. SHADING (v0.41.0) — NEVER-RAISE: command = min(current_position, target),
   unknown position skipped (CoverInfo.current_position new); Via/Vacanza →
   ALL unblocked shadeable covers to 0 (unknown position still closes);
   SHADING_AZIMUTH_BANDS south widened (135,225)→(135,270) ("south" label =
   real SW facade ~225°; old band released at afternoon peak sun — studio_v
   27.6°C despite 90% fan).

Forensics resolved: number.main_bedroom_shade_position never went 50→80 — the
cover raises came from the policy's own proportional target via bidirectional
set_position (fixed by never-raise). Owner set that number to 25 on 7/4 12:44.
No HA automation touches cover.grande_camera (the 22%/100% counter-writes were
human wall presses).

TOP PRIORITY NEXT — S_eff per-facade solar (owner ask 3/7, backlog Pri 2b),
deliberately NOT bundled with the defect train: replace regressor b·S_ghi with
b·S_eff = GHI × f_geom(sun elev/azimuth vs facade normal from cover labels;
COMPUTED not learned; SW=225°/WNW=292° — reuse the rotated-facade fact from
SHADING_AZIMUTH_BANDS) × g(cover position; ~0.2 floor closed; multi-cover rooms
sum per facade). Same 4 learned params (S_eff is an input transform); k STAYS
constant (water-side; the apparent "k(t)" at peak is G error misattributed).
Touchpoints: supervisor/thermal regressor + ZoneSnapshot/thermal buffer needs
per-zone facade geometry + cover positions each cycle (CoverInfo.current_position
now exists) + planner per-room sim (currently un-projected house-level curve).
Design + adversarial-review workflow FIRST (model changes corrupt learning
silently); wipe/decay the learned b on migration (it was fitted to GHI).

THEN (backlog order): per-room occupancy/room-roster (ep_occ mapped, no consumer;
Gabriele case), "free-air/windows-open" mode (Via does NOT stop cooling —
pre-cool/PV compose below the mode center; Vacanza is the workaround), night-guard
threshold (fan ran all night at 26.3 vs threshold 26 in an empty room), VMC boost
away free-cooling (vmc_cucina_e_casa entities currently unavailable), fix
group.presenza_adulti (unknown → away escalation inert), per-room setpoints, KPI
energia, split trio, legacy cleanup → v1.0.0.

TIER-1 TRAIN (renumbered; still STOP-gated on the live soak, now of v0.41.0):
P3 delete-trio (exact-value pin rule; the trio still carries tonight's law
changes verbatim) → P4 feature_graph → P5 R2 deviation-space → P6 R3 quorum.
unified_planner + regime enabling stays gated (mild weather + k-convergence).

RULES unchanged: strict deploy-dark for anything new actuating; small commit +
tag + gh release per increment; pytest (1343) + ruff green on the pinned target
(pytest-homeassistant-custom-component==0.13.324 = HA 2026.4.3 / Py 3.14);
fail-safe invariants; HA connector read-only for diagnosis; owner deploys via
HACS update + restart. Known quirks: ~40s KNX unavailable blips (nightly ~03:00
backup + occasional daytime) — ignore singles, ThermalEstimator skips them.
```
