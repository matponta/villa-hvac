# Next session — kickstart prompts

## ✅ DONE — dead-fan-at-wake hotfix (v0.56.0, shipped 2026-07-12)

The 2026-07-12 incident (padronale a DEAD ZONE 09:31→16:04, hvac_levers=0
throughout — a mild night meant the #2b heat-guard never fired, so nothing
re-armed the fan the 00:11 silence had switched OFF) is FIXED in **v0.56.0**.

NEW VERIFIED FACT recorded in CLAUDE.md: a KNX fancoil in AUTO does NOT restart
a fan whose switch object was written OFF — only an explicit ON revives it — and
the interlock holds the EV valve CLOSED meanwhile, so a `manuale`-only hand-back
strands the zone INVISIBLY.

Three fixes shipped (all with tests, mutation-verified, pre-tag adversarial
review):
1. `NightSilenceController._release` emits a one-shot fan turn-on
   (`NIGHT_GUARD_FAN_PCT`) for a bedroom the silence left OFF, SKIPPING
   #4-paused / free-cooling zones; a guard actively cooling at hand-back already
   has a live fan → no turn-on (guard-fired release byte-identical).
2. `async_fail_safe` re-arms any fan the supervisor switched off this session
   (tracked in-memory in `_fans_turned_off`, populated in `_dispatch_write`);
   fail-safe SHA pin updated per the test's own protocol.
3. Engine self-heal watchdog `_stranded_fan_watchdog` (closes the whole class):
   a cooled leader we are NOT managing (manuale off) whose fan reads OFF + valve
   CLOSED while genuinely above its live thermostat setpoint (summer, enabled,
   un-paused, not free-cooling, not a Notte bedroom) for `STRANDED_FAN_CYCLES`
   (10) → one `fan.turn_on` (retried every 10 cycles) + WARN once. Gated on temp
   > setpoint so it can never fight a legitimate satisfied-stop.

⚠️ AFTER THE OWNER DEPLOYS v0.56.0 (live-verify, read-only HA connector): next
morning confirm **manuale OFF at 08:00 AND the fan alive** (or the valve opening
on first demand) on BOTH bedrooms; and check **gabriroom retroactively** for the
same dead pattern on 7/12 (was it also silenced mild + stranded?).

## Backlog session prompt (after the hotfix)

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

STATE (2026-07-12): repo == v0.56.0 (1516 tests, ruff clean). LIVE = v0.55.0 —
DEPLOYED 2026-07-12 00:42 (HACS update from v0.53.0 + restart). v0.56.0 is the
dead-fan-at-wake HOTFIX (see the DONE section at the top of this file) — NOT yet
deployed; owner deploys via HACS + restart. AFTER DEPLOY verify: manuale OFF at
08:00 AND the fan alive on BOTH bedrooms; the padronale valve opens when the #2b
heat-guard fires; open a window and watch its zone pause.
⚡ THE SUPERVISOR IS LIVE: switch.supervisor + auto_setback + vmc_auto + split_ac
+ free_air ON — the engine ACTUATES the villa. Any change to the
#2/#2b/#4/free-air/VMC paths changes LIVE behavior the household relies on
nightly. Small increments, pre-tag adversarial review, never weaken the
fail-safe invariants.

v0.54.0 (#2b HEAT-GUARD CHILLED WATER — closed the top owner backlog item):
guard-active now ALSO slams the bedroom setpoint to threshold−0.5
(NIGHT_GUARD_SETPOINT_DROP; summer only, bounded ≤ a COMPUTABLE #2a mode
target — never raise, skips disabled/paused/free-cooling zones) so the EV
valve opens and the held 33% fan moves CHILLED air — the legacy guard
circulated warm air valve-CLOSED in the 26–27 dead-band (padronale's whole
first night). Releases: guard 10-min-below hysteresis / auto-wake / Notte exit
(#2a re-asserts in the same merge) + async_fail_safe restoring the NUDGE-TIME
snapshot (night.failsafe_setpoints — deliberately NO live entity reads: the
select/number platforms are torn down before an unload-path fail-safe; found
as a MAJOR by the 21-agent pre-tag adversarial review, along with the
free-cool interplay). The fail-safe SHA pin in test_engine was updated
deliberately, per that test's own protocol. Golden tests pin the legacy
silence/release/fan behavior; an engine-level test proves chilled-water
delivery through the real cycle (mutation-verified).

v0.55.0 (WINDOW CONTACTS — owner ask 2026-07-11, STORY_WINDOWS.md): 6 Shelly
BLU contacts wired as #4 `window` keys (main_bedroom, gabriroom, studio_v =
binary_sensor.aaa_window, office, ingresso radiant, Porta Cucina → living_room
LEADER — kitchen has no thermostat, open space); #2b guard now fan-0 on paused
bedrooms (old warm-air edge CLOSED); windows→free-cool inference behind opt-in
switch.windows_free_cooling (default OFF: ≥3 contacts open + outdoor ≤ leaders'
mean − 1.0 °C → ORed into the ONE _is_free_cooling; policies duplicate
deleted); long-open contact alert (30 min, once/episode, Italian push to
matphone16 + pixel_10, suppressed while deliberately airing; vasistas never
page). Options: windows_free_cool_count/margin + window_alert_minutes.
NOT wired: cantina_impianti door (plant room), up_sense_contact (unmapped).

FIRST NIGHT (7/10→11) verified clean from the recorder: mode bridge propagated
Chiudi-notte in 11 ms; #2b silence latched same-second; heat-guard fired 00:17
(3-min debounce); auto-wake released at exactly 08:00:00; hvac_levers=0 all
night; zero errors; VMC2 bedroom unit stayed silent (night-quiet veto held),
VMC1 flushed once 00:40–00:50. ENABLED since (owner; confirmed live 2026-07-12):
split_ac + free_air — both OFF on this first night, turned on afterward. Still
OFF (owner's pace): fan_pacing, duty, pv_bias, free_cooling, windows_free_cooling,
unified_planner, regime, seff.

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

1. PER-ROOM OCCUPANCY ROSTER (#2 evolution): every zone has ep_occ mapped in
   ZONES but NOTHING consumes it (only #6 palestra comfort reads occupied).
   Goal: a vacant room relaxes toward its own setback (the Gabriele case:
   empty bedroom on comfort schedule all day). EP occupancy is flappy →
   debounce/latch like presence. Design first (small story doc): per-zone
   opt-in? offset-based relax vs preset? interaction with #2b bedrooms +
   comfort windows (F4b).

2. #2 OFFSET INTO plan_center_schedule: the per-room offset (v0.49.0) is
   applied at resolve_center/house_mode/precool but NOT in the unified-planner
   schedule base — a HARD GATE before switch.unified_planner can ever be
   enabled. Mechanical, well-scoped.

3. FREE_AIR → PER-ROOM "OPEN WINDOWS" (owner ask, PARTIALLY superseded by
   v0.55.0): the 6 main rooms now have REAL contacts wired into #4. Remaining:
   per-room manual switches for the sensor-less cooled rooms (salotto windows,
   sala_giochi, rack) + the free_air rename — decide whether still wanted now
   that contacts cover the main rooms.

4. LEGACY CLEANUP → v1.0.0: after ~1 week of clean supervisor nights, DELETE
   the disabled automations + the buonanotte/sveglia scripts' climate branches
   + automation.sistema_ricrea_group_presenza_adulti_all_avvio (obsolete since
   v0.46.0 watches person.* directly). NOTE: the 3 notte_guardia_caldo_camera_*
   automations are now doubly-superseded (v0.54.0 guard does fan AND valve).
   Consider rewiring the physical buttons to select.house_mode directly and
   retiring the bridge last.

5. TIER-1 TRAIN (STORY_TIER1): P3 delete-trio is STOP-gated on the live soak
   of the merged CoolingController — the soak STARTED 2026-07-10 when the
   supervisor went live; give it 1–2 clean weeks (watch hvac_levers + nightly
   behavior), then P3 → P5 (R2 deviation-space) → P6 (R3 REST-quorum + boot
   manuale sweep). P4 feature_graph already shipped (v0.47.0).

6. OUTSIDE-AIR MERGE (free-cooling × open-windows × VMC, owner ask): the
   v0.55.0 windows→free-cool inference is the merge's FIRST CONCRETE PIECE
   (owner's own rule). Remaining design after live data: free-cool conditions
   + occupied → "open the windows" notification; VMC interplay; free_air fold.

Smaller follow-ups (v0.55.0 review deferrals): window-pause vs auto_setback
gate (rule 1 inert when setback off — page warns honestly; exemption needs a
restore-path redesign) · window-alert quiet hours / bedroom-during-Notte
deferral (owner decision) · close→restore debounce if wind-flap observed ·
grouped digest for multi-window pages · VMC thresholds (const → options flow) · #6
cycles_since_restart resets on restart (total is restored — cosmetic) ·
night-guard threshold tuning on real nights (26.0 default) · watch the first
v0.54.0 guard night: valve open minutes + did the room actually get driven
below threshold (25.5 target) without overshoot complaints · watch the first
v0.55.0 window events: pause/restore latency, alert timing, no page spam ·
owner may flip switch.windows_free_cooling when ready (default OFF).

LIVE-OPS for the owner (not build work — support if asked): watch S_eff
geometry on the Modello tab → flip seff_enabled → STORY_SEFF §8 gates ~1wk;
fan_pacing daytime trial (lowest-risk next lever; also unlocks pv_bias);
free_cooling opt-in when wanted; split_ac DONE — LIVE
(automation.circolazione_aria_cantina_vini confirmed disabled ✓);
windows_free_cooling only AFTER the first clean nights of v0.55.0 raw window
pause/alert behavior.

RULES unchanged: pytest + ruff green on the pinned target
(pytest-homeassistant-custom-component==0.13.324 = HA 2026.4.3 / Py 3.14);
small commit + tag + gh release per increment; pre-tag adversarial review
(workflow if budget allows, else inline 3-lens + refutation); fail-safe
invariants byte-preserved (the SHA-pin test's own update protocol applies);
HA connector for live diagnosis (read-only unless the owner asks);
owner-visible behavior changes get a household-manual update (artifact
8d1ef72b + PDF in repo root — v0.54.0 manual update PENDING, do it from the
Cowork session that owns the artifact). Known quirks: ~40s KNX unavailable
blips (nightly ~03:00 backup) — ignore singles; the ThermalEstimator gap guard
covers the long ones.
```
