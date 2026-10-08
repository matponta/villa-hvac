# Next session — kickstart prompts

## v0.82.0 — winter tuning from the first live day (2026-10-08)
Live check 8/10 13:50: Condominio battery WORKS (0 % at dawn → 32 % at 14:30,
charging ≤ 6.8 kW, grid ~0) but with 41.4 kWh starting empty SoC 90 % comes late
or never → PV heating idle all day. Only one radiant valve opened (bagno P1
07:30–07:49, consenso caldo followed 07:31–07:49); house at 21–22 °C, outdoor
15–19, house in Via from 07:51 (comfort/19, P1 18). Winter model: loss a learned
in 15/16 rooms after one night = 0.005–0.021/h (well under the 0.03 prior; several
pinned at the old 0.005 floor); bagno_giochi learned a bogus 5-min lag (vasistas
pause ended 07:30 → air recovery). KNX went unavailable ~1 min 5× overnight.
Fixes (owner "facciamo tutti e 3"): (1) no winter learning while #4-paused and
60 min after; sub-15-min lags discarded on load; (2) BOUNDS_A floor 0.001, lag
floor 15 min; (3) PV early start: net charge ≥ 1.5 kW AND PV left today ≥ 1.3 ×
energy to fill the battery (`battery_fills_today`; capacity
`sensor.battery_capacity_2` = kWh despite the "kW" unit); stay below 85 % while
it still fills. Live 8/10 14:30 (32 %, 10.7 kWh left) correctly = no start.


## v0.81.0 — WINTER BRAIN W2–W4 (2026-10-07) — LIVE (owner restart ~20:40)
Live check 7/10 evening: all 16 `*_inverno` sensors populated (rooms 21.2–22.8 °C,
all at/over target → 0 min), plan=idle, `switch.pv_heating` turned ON by Claude
(owner asked), `sensor.riscaldamento_fv`=idle/no surplus (SoC reads 0 % at night —
verify `sensor.battery_percentage_2` at midday), return date still 8/9 → no advice.
Dashboard CoolClima → Casa: new winter-only section "Cervello invernale" (tempo,
riduzione/rientro entities + table, PV switches + status). Entity ids: master
`switch.pv_heating`, per room `switch.<name>_pv_heating`, `switch.return_pre_cond`.
Owner decisions 7/10: PV rooms = camera padronale, bagno padronale (01+02),
living room → their CASA target; surplus = battery > 90 % AND a real PV surplus
judged on NET Condominio flows (other apartments share the PdC). Built per
`STORY_WINTER_BRAIN.md` "As built"; adversarial review → 5 MAJOR fixed before
release (PV latch stuck on missing data; lift permanent with Auto setback off;
start ignored grid import; one economy room zeroed the W3 depth; W3 dropped to
Via at the ETA) + the pre-existing summer #8 UTC-ETA bug + minors (hand-back
re-asserted 10 cycles; lag only from an observed opening; floor ≥ 15; dead
sala-giochi fan excluded from fancoils-off). 798 tests.
AFTER THE RESTART: verify `sensor.tempo_riscaldamento` / `*_inverno` exist; turn
ON `switch.pv_heat` (owner asked for it); W3 needs `switch.return_precond` ON +
arming via the "quando torni?" push or the date/daypart entities. Add a winter
dashboard card (tempo riscaldamento, riduzione consigliata, riscaldamento FV).
Note: v0.80.0 was downloaded AFTER the 19:52 restart, so live is still v0.79.0.


## v0.80.0 — WINTER BRAIN W1: radiant observer (2026-10-07)
Spec: `STORY_WINTER_BRAIN.md` (owner goals: time-to-temperature, how deep a
weekend setback can go, PV heating of chosen rooms even while away — all via
thermostat SETPOINTS). Owner created the 15 radiant valve STATE sensors
(`binary_sensor.radiante_*_valvola`, read-only) → `HEAT_VALVES` in const.py
(bagno padronale shared by 01/02; pianerottolo_p2 none). Dashboard: CoolClima →
Diagnostica got a winter-only "Valvole radiante" section (tiles + 24 h graph with
the heating call), added live 7/10.
W1 = pure `supervisor/winter_model.py` (`WinterModel`: loss a on long
valve-closed low-sun windows after a 90 min slab residual; heating rate k_h on
valve-open windows after the lag; lag by back-extrapolating the measured climb;
running means, priors a 0.03/h · k_h 0.6 °C/h · lag 45 min; own Store
`villa_hvac_winter_models`) + `engine._observe_winter` (winter only, never
actuates) + `sensor.<room>_inverno` (minutes to the room's comfort target) +
`sensor.tempo_riscaldamento` (slowest room). NEXT: W2 setback advisor, then W3
winter return pre-conditioning, W4 PV heating (see story). Valve signals not yet
seen moving live (all closed at 21–22 °C on 7/10) — check the first heat call.


## v0.79.0 — winter: fancoils always OFF (2026-10-07)
Owner 7/10: central plant switched to INVERNO (salotto `heat`, s5a Inverno,
`binary_sensor.impianto_inverno` on, consenso caldo ON). v0.78.1 was already live
and the winter light took over cleanly: Casa → 16 thermostats `comfort`/21
(P1 20 with its −1 trim), lavanderia BP (vasistas window pause), plan=`heating`.
Owner rule: in winter ONLY the radiant floor heats (thermostat-regulated); the
fancoils must stay OFF. Live: all 8 fans sat ON at 0 % in AUTO after the
changeover, valves closed — and the `fan:` % lever reads ON-at-0 as "0" =
satisfied, so it could never assert OFF. → new `fan_power:` lever (switch object
only) + `WinterFancoilController` (winter.py): winter → all 8 fancoil fans OFF
(arbiter re-asserts; a wall press concedes 2 h then off again); first non-winter
cycle → one ON each. `fan_power` OFF discards the fan from `_fans_turned_off` and
the engine clears it in winter, so the fail-safe won't spin fans up on a winter
unload. In-memory: after a summer restart the summer watchdog revives fans.

BACKLOG (owner 7/10): **winter rack cooling with the rack fancoil FAN only**
(no chilled water in winter — ventilation of the rack room / P1). Today the rack
guard is summer-gated and the rack fan is held OFF all winter → watch
`sensor.rack_temperatura_media` this winter; design a winter rack guard that
may run `fan.fancoil_locale_rack` (exception to WinterFancoilController) when hot.


## v0.76.0–v0.78.1 — WINTER LIGHT (2026-09-30) — RELEASED v0.78.1, NOT DEPLOYED
(LIVE at the time = v0.75.2, installed by the other 30/9 session; house in Vacanza,
all 17 thermostats still `cool` + BP, `sensor.s5a_stagione` = Estate.)

Read-only winter audit first (code + live). Findings that drove the releases:
free_air (ON, restored) would have held the 7 fancoil-labelled thermostats in BP
all winter; the ONE house slider sat at 24.5 → would have been written as a
HEATING setpoint to every radiant floor; #8 return-precond lead is cooling-only
(house in BP the whole absence, ramp 30 min before ETA); #2b silenced bedroom
fans nightly; the estimator would fit radiant heat into {a,b,c}; season blind
default = SUMMER. Nothing reads EV HEAT valves / consenso caldo (not needed).

- **v0.76.0 safety**: free_air, #2b night_active, #8 (+ its Via ask) and the F2
  estimator are summer-only; season fallback = last conclusive → calendar
  (15 Oct–15 Apr).
- **v0.77.0 winter light** (owner decisions): heating ONLY via thermostat
  setpoints; winter = preset `comfort` for Casa/Via/Notte (Vacanza keeps BP);
  separate `number.house_setpoint_winter` (default 21); Notte −1 / Via −2;
  `number.<zone>_offset` + `switch.<zone>_economy` (−3, option
  `winter_eco_offset`) on ALL 17 thermostat zones; writes clamped [15, 25].
- **v0.78.0 winter sun**: `switch.winter_sun` (default ON) — winter + Via/Vacanza
  + sun on the facade + gw3000a ≥ `winter_sun_solar` (150) → open that facade's
  covers once/day; at sunset restore ONLY the ones we opened; hand-moved covers
  are the owner's. Edge-triggered, outside the arbiter, Store-persisted.

- **v0.78.1 review fixes** (adversarial review: 0 blocker/major, 5 minor): a
  stop at home no longer forgets the opened covers (sunset still puts back ours,
  any mode; a shade-blocked room is hands-off); failed sunset put-back retried
  (≤10); takeover margin 15 % (a cover stopping at 92 is not "the owner");
  winter #2b release + fan-actuator re-arm don't spin bedroom fans in heat mode
  (deferred to summer); last conclusive season PERSISTED (`villa_hvac_season`
  Store) and winter-sun holds until a LIVE season signal; its cycles are entry
  background tasks with a stop flag. Known, not fixed: fail-safe restores
  BP→auto but not setpoints, so an unload during winter Via/Economy leaves the
  setback on the thermostats until the next write (existing behaviour).

DEPLOY CHECKLIST (owner):
1. HACS update → v0.78.1 + restart. Verify loaded, BLOCCO off, levers 0.
2. **Options flow: set `winter_notte_offset` −4 → −1** (the live entry stores −4
   explicitly, so the new default does NOT apply by itself).
3. Set `number.house_setpoint_winter` (default 21) BEFORE the flip.
4. Retire/disable the legacy `script.alza_riscaldamento` / `abbassa_riscaldamento`
   (20/18 °C on all 17) — they'd fight #2a (re-asserted, then 2 h concede).
5. At the first real heat flip (salotto → `heat`), LIVE-VERIFY: KNX `comfort`
   + our temperature is what the radiant regulates; consenso caldo follows; the
   summer slider is no longer written; bedroom fans untouched in Notte.
6. First sunny winter Via: covers on the sunny facade open once, back down at
   sunset (INFO log "Winter sun: …"). Note the living-room `tenda_*` (blind,
   south) are in the #6 map too — they will RAISE.
Open: Palestra split turned `off` in winter by the split trio if used in heat;
#7 pre-heat (anticipatory radiant) still not built — the natural home for the
F4c planner in winter.


## v0.70.0 — VMC boost: outdoor-cap hysteresis + duration cap (2026-09-10) — DEPLOYED
(HACS v0.70.0 + restart 10/9 19:31; loaded clean, `update.villa_hvac_update` installed_version
= v0.70.0, BLOCCO off, hvac_levers 0, no villa_hvac errors in the log.)

Owner question: "why is the VMC boost on so often, who turns it on?" Answer:
`switch.vmc_auto` (#5), not any HA automation — proved three ways on 10/9.
(1) 14:09:49 outdoor 23.9→24.2 crossed `VMC_BOOST_OUTDOOR_MAX` → boost OFF at
14:09:57, one 30 s tick later. (2) 07:30:32 `select.house_mode` Notte→Casa lifted
the night-quiet veto → ON at 07:30:45. (3) 07:57:57 an integration reload's
`async_release` wrote OFF and the next tick (07:58:27, +30 s exactly) wrote ON.
Everything else was ruled out: `automation.vmc_bagno_padronale` is DEAD (triggers
on `sensor.statistical_characteristic`, which no longer exists; last_triggered
2026-03-03), `script.30_min_vmc` never ran, `timer.vmc_boost_bagno` idle since
4/9 (BILRESA unused), and `automation.bagno_padronale_bilresa_vmc_boost_tapparella`
only REACTS to the switch going off (trigger id `spento_a_mano`) — its
last_triggered lands 0.2 ms AFTER the state change. Manual events do exist and
are distinguishable: villa_hvac writes carry `context.user_id = null`, and the
18:33:19 OFF on 10/9 had a real user_id.

Measured 3-10/9 on `switch.vmc_boost`: 38 h ON of 178 h = **21% duty**, 19 starts,
stints of 11 h (3-4/9 22:31→09:34), 8.6 h (8/9) and 6.2 h (10/9), plus 1-16 min
cycles on 8-9/9 when outdoor oscillated around the hard 24.0 cap.

Change (both in `const.py` + pure `supervisor/control_law.py`, wired in `vmc.py`):
`VMC_BOOST_OUTDOOR_HYSTERESIS` 0.5 (start < 24.0, stop ≥ 24.5) and
`vmc_boost_step`/`VmcBoostState` with `VMC_BOOST_MAX_ON` 4 h → `VMC_BOOST_COOLDOWN`
1 h → re-arm, plus `VMC_BOOST_MIN_ON` 15 min. Quiet veto + disable/unload still
win over MIN_ON; an owed cooldown survives a veto; a manual boost is untouched.
Tests: 14 new (11 pure + cap/cooldown/min-on + one freeze_time end-to-end).

Deploy: HACS update to v0.70.0 + restart. Verify: next cool night the boost runs
≤ 4 h then rests 1 h (INFO log "cap reached, resting until …"), and no more
sub-quarter-hour cycles around 24 °C. NOT addressed yet (owner backlog): the
`indoor` term is `max()` over the served zones, so the kitchen at 25-26 can still
authorise a boost that pushes 23-24 °C air into cooler bedrooms while the fancoils
cool — the daytime 07:58→14:09 stint of 10/9 was exactly that. Also still open:
VMC 2's native integration (10.5.152.105) is unavailable since 4/9, so the KNX
boost switch is driven with no airflow feedback.

## v0.69.0 — Via no longer full-closes the covers (2026-09-05) — DEPLOYED
(live check 10/9: `update.villa_hvac_update` installed_version = v0.69.0)

Live 5/9 08:15:59: `select.house_mode` → Via and within 0.7 s all 11 shadeable
covers went `closing` (grande/piccola camera, studio_v, grande studio, Somfy
tende…). The owner reopened with "Apri Casa" at 08:16:49 and 08:17:49; the
arbiter conceded a manual override and re-closed them at 10:17:53 — exactly
`DEFAULT_OVERRIDE_BACKOFF` (2 h) later. No HA automation was involved: it was
`shading_policy`'s v0.41.0 (081dafd, 2026-07-04) "Via/Vacanza full-close".

Decision (owner): full-close only in **Vacanza**; **Via** = ordinary shading
(band + irradiance + never-raise), identical to Casa. Via is the short-absence
mode (errands, airing the house) — slamming the house shut on selection fought
the owner. Change: `policies.py` `shading_policy` (`house_mode == VACATION`
branch only); tests `test_shading_vacation_closes_everything` (renamed) +
`test_shading_away_is_normal_shading_not_full_close` (new).

Deploy: HACS update to v0.69.0 + restart. Verify: select Via on a bright
afternoon → only the sun-facing covers move, to their per-room target; at dusk
nothing closes. Vacanza still closes everything.

## v0.68.0 DEPLOYED — orphaned guard `manuale` reaped at boot (2026-08-16)

Found while verifying the v0.67.0 deploy. After the restart both guards were
inert (their in-memory latches are empty by construction at boot) yet
`switch.fancoil_locale_rack_manuale` and the office one were STILL ON from the
pre-restart episode — so the rack fan stayed pinned at 67 % in manual with no
lever claiming it. Each guard's `_release` short-circuits on the empty latch, and
`_stranded_fan_watchdog` is deliberately blind while `manuale` is ON, so nothing
would ever have handed it back.

Fix: `engine.async_release_orphan_guard_manuals()`, called from
`_startup_resync`. Release-only, fan left alive, scoped to the two guard-owned
switches (`GUARD_MANUALE_SWITCHES`) — the governor (living_room) and #2b
(bedrooms) can legitimately hold theirs at boot, so they are untouched. A guard
that still wants the switch simply re-asserts it on its next cycle.

Also corrected: `_startup_resync` only released BLOCCO + re-applied presets; two
comments in `engine.py` described a broader boot resync than existed.

660 tests, ruff clean. Both new tests mutation-verified.

VERIFIED LIVE 2026-08-16 15:40:38 — the boot resync logged both releases and
flipped both switches ON→OFF 34 s after boot; post-boot state is clean (no
fancoil in manual anywhere, BLOCCO off/allow, plan ticking, levers=0).

Observed at the same time (worth knowing): with everything back in AUTO,
salotto/cucina/rack sat at 67 % while padronale/gabriele/sala_giochi/office sat
at 100 % — so AUTO MODULATES across the 33/67/100 stages. The old "AUTO runs
~constant 100 %" fact holds only while the valve is pinned open under load.

## v0.67.0 DEPLOYED — guard fan % is a FLOOR (2026-08-16)

Live regression found by the owner returning from vacation: on a 35.7 °C peak day
`P1GuardController` had latched and was holding the **office fan at 67 % with the
room at 28.8 °C**, while every AUTO-driven fan in the house ran 100 %. Root cause:
asserting `manuale` ON takes a fan out of KNX AUTO (~100 %) and pins it at exactly
the commanded %, so the flat `P1_GUARD_FAN_PCT = 67` made engaging the guard a
THROTTLE, not a boost. The rack fan escaped only because the rack guard had
separately escalated it to 100 (via the no-response route — the wide 35 °C band
means the +2 emergency rise never fired).

Shipped:
- pure `guard_fan_pct(base, running, escalated)` in `rack.py` — never command below
  the airflow already running; used on the active AND hand-back paths of BOTH
  guards (the hand-back previously stomped a 100 % fan with 67 on the way out).
- `P1GuardController` now uses the escalation ladder it already computed but
  ignored (it shares `rack_guard_step`) → 100 % at P1 ≥ threshold+2 for 3 min, or
  20 min without a 0.3 °C improvement. Before this it had NO path to full airflow.
- 5 new tests, all mutation-verified to fail under the old flat-constant behavior.
  658 tests, ruff clean.

Note: the guards' *setpoint* nudges are no-ops whenever the room is already well
above its own base (`min(base, temp − 1)` collapses to `base`) — harmless, the
valve is open anyway, but it means on a hot day the guard's ONLY real lever is the
fan %. That is why the throttle was the whole story.

## v0.66.0 — rack guard rework (2026-08-16, Cowork session)

Changes (owner-requested):

- **Rack probe**: `ZONES["rack"]["temp_sensor"]` → `sensor.rack_temperatura_media`
  (template helper already LIVE in HA: mean of the two Tuya probes
  `rack_t_h_temperature` + `t_h_rack_new_temperature`, skips a silent probe,
  holds last value when both are silent). Rack zone gets a dedicated
  `stale_after` = 180 min (both probes report every ~78–120 min; the global
  30-min freshness blinded the guard — root cause of the 24–28/7 blindness).
- **Rack guard engages in ANY house mode** (Vacanza included): the
  `house_mode != Vacanza` eligibility gate is removed from `RackGuardController`
  only (P1 guard unchanged). While active in Vacanza the guard lifts the P1
  preset to `comfort` (BP ignores setpoints) and nudges `p1.temp − 1` with no
  base cap (`mode_offset` is None in Vacanza); on release house_mode_policy
  re-asserts building_protection the same merge cycle.
- **Wide band**: engage 35 °C (option `rack_temp_threshold`, default now 35,
  clamp 24–45), cool down to 28 °C (NEW option `rack_temp_release`, default 28,
  clamp 20–40). `rack_guard_step` gained a `release_drop` param (default 1.0 —
  P1 guard behavior unchanged); controller enforces drop ≥ 1 °C.
- Feature-graph rack inert reason now distinguishes "rack temperature
  unknown/stale" from "below activation threshold" (the 12/8 misleading string).
- Options flow + translations (en/it/strings) updated; manifest 0.66.0.
- Dashboard cool-clima-v2 already repointed LIVE to `sensor.rack_temperatura_media`
  (5 refs, was `sensor.locale_rack_temperature`).

Deployed as part of v0.67.0. `rack_temp_threshold` was already set to 35 in the
live entry options (2026-08-16, via options flow — interim behavior on v0.65 =
engage 35 / release 34); `rack_temp_release` is not set and correctly falls back
to the new default 28.

## v0.64.0 INSTALLED — HA rebooting (owner report, 2026-07-15)

Release `v0.64.0` / commit `6069baf` is published and installed through HACS. The
owner reported HA rebooting after installation. Local verification before release:
642 tests passing and Ruff clean. The next session begins with post-reboot entity and
fail-safe verification, then performs the dashboard migration below. Do not repeat
the implementation or release.

## NEXT SESSION: climate-dashboard migration

### 1. Post-reboot safety check before editing the dashboard

Confirm these entities exist and have the expected restored state:

- `switch.supervisor`: retain the owner's existing restored state;
- `switch.main_bedroom_night_silence`: ON by default/restored;
- `switch.gabriroom_night_silence`: ON by default/restored;
- `switch.rack_guard`: ON by default/restored;
- `switch.steady_pacing`: OFF by default/restored;
- `switch.paced_living_room`: OFF by default/restored;
- `sensor.hvac_room_living_room`: available;
- `sensor.locale_rack_temperature`: available;
- `switch.unified_planner`: MUST remain OFF.

Also confirm the boot fail-safe left `switch.ct_blocco_freddo_villa` OFF/ALLOW and
that no fancoil `manuale` switch was stranded ON by the restart unless an active
owner-approved controller presently owns it.

### 2. Snapshot before dashboard edits

Export/copy the current climate-dashboard raw configuration before changing it. Do
not overwrite unrelated lighting, shutter, scene, return-home, VMC, window or house-
mode controls. Record the exact old card containing the Padronale/Gabriele
Buonanotte/Sveglia buttons so rollback is one paste.

### 3. Replace only the old bedroom climate buttons

Remove the old *momentary climate* Buonanotte/Sveglia buttons and any dashboard-only
`input_boolean.notte_silenziosa_*` controls. In the same location insert:

- `switch.main_bedroom_night_silence`, labelled **Camera padronale**;
- `switch.gabriroom_night_silence`, labelled **Camera Gabriele**.

These are persistent selectors, not immediate sleep actions: ON means that bedroom
participates whenever `select.house_mode` is `Notte`; OFF excludes it. Changing a
selector during Notte takes effect immediately. Morning/Notte exit still wakes every
room that actually participated, releases manual mode, and explicitly re-arms a fan
left OFF by silence.

Do not yet delete the underlying legacy Buonanotte/Sveglia scripts or automations.
That cleanup is a separate HA-side operation after snapshotting and one verified
night; preserve all unrelated light and cover actions.

### 4. Add the living-room governor card

Add the second card from `dashboard_v0.64.0_cards.yaml`:

- `switch.steady_pacing` — **Osserva regolazione**;
- `switch.paced_living_room` — **Applica al salotto**;
- `sensor.hvac_room_living_room` — **Stato e spiegazione**.

Safe rollout sequence:

1. Leave both switches OFF while checking the upgrade.
2. Turn ON only `steady_pacing` to enter SHADOW: calculations/card update, no fan or
   thermostat writes from the governor.
3. Keep `paced_living_room` OFF until the owner has reviewed the shadow explanation.
4. Only after explicit owner acceptance, turn ON `paced_living_room` to actuate.
5. If comfort, data, noise or legibility is wrong, turn OFF `paced_living_room`; the
   controller hands both Salotto and Cucina fans alive back to AUTO.

Never add pacing controls for other rooms. Never enable `unified_planner` in this
session.

### 5. Add rack protection visibility

Add the rack card from `dashboard_v0.64.0_cards.yaml` containing:

- `switch.rack_guard`, labelled **Protezione rack**;
- `sensor.locale_rack_temperature`, labelled **Temperatura rack**;
- `sensor.hvac_plan`, labelled **Diagnostica HVAC** (its `feature_graph` contains the
  rack guard's active/inert reason and command details).

Rack guard should remain ON. It engages above 28 C for 3 minutes, starts the shared
fan at 67%, and may escalate to 100% only as hardware protection.

### 6. Dashboard acceptance check

- Toggle each bedroom selector once outside Notte and verify it persists after a
  dashboard reload; restore the owner's desired ON/OFF selection afterward.
- Verify the living-room sensor state is one of `native`, `shadow`, `paced`,
  `escalated`, or `demoted`, and its `explanation` attribute is readable in Italian.
- Verify the rack temperature and guard state are visible.
- Verify the old climate buttons are gone but unrelated Buonanotte lighting/shutter
  controls remain.
- Save a post-edit raw-dashboard snapshot and record the dashboard URL/view name.

The ready-to-paste YAML is [`dashboard_v0.64.0_cards.yaml`](./dashboard_v0.64.0_cards.yaml).

## ✅ v0.56.0 LIVE + VERIFIED (read-only probe 2026-07-15)

v0.56.0 was deployed 2026-07-13 12:02 (HACS + restart). The wake re-arm is PROVEN
live: 2026-07-15 07:30:31 the supervisor released gabriele's manuale at .651 and
one-shot fan-ON at .656 (the silence had left it OFF); padronale's fan was already
alive from a guard cycle → correctly no write. Mornings 7/14 + 7/15 clean. The only
dead-fan episode in the 7/12→7/15 window was 7/13 07:45→07:48 under v0.55.0 (the
already-fixed bug; owner re-armed by hand).

**Buonanotte verdict (owner hypothesis, probed 7/15):** the legacy path IS alive and
one-sided — `script.buonanotte_padronale` fires nightly, TWICE per Hue-remote press
(`automation.telecomando_hue_bedroom` calls it directly AND again via
`input_button.chiudi_notte` → `automation.spegni_tutto_e_chiudi`; the second run
rejects as failed_single). It writes manuale ON + fan OFF + latches
`input_boolean.notte_silenziosa_*`, and the legacy WAKE side is entirely disabled
(and never re-armed the fan even when it ran) → the booleans latch ON forever.
NOT the current strander (the supervisor writes the same state ~1 s later and
v0.56.0 heals any OFF fan regardless of author), but a real residual risk: with the
supervisor master OFF overnight, NOTHING re-arms padronale; and the watchdog is
blind while manuale stays ON. ⇒ Fix-pack item 1. Effective wake is 07:30 via
`smart_wakeup`→Apri Casa most days; the 08:00 auto-wake is the fallback path.

## FIX PACK session prompt

```
Resume work on villa_hvac — FIX PACK session.
CWD: /Users/mattia/Documents/Claude/Projects/Home Assistant/villa-hvac
Read CLAUDE.md in full first (verified facts). MASTER_PLAN.md = build checklist.
STATE: repo == LIVE == v0.56.0 (1522 tests, ruff clean). Supervisor LIVE + actuating
(supervisor/auto_setback/vmc_auto/split_ac/free_air/windows_free_cooling ON;
fan_pacing/duty/pv_bias/free_cooling/unified_planner/regime/seff OFF).
Small increments, pre-tag adversarial review, fail-safe invariants byte-preserved.

THE OWNER-APPROVED, RELEASE-BY-RELEASE SPEC IS:
IMPLEMENTATION_PLAN_FIX_PACK_PACING_V3.md. Follow it exactly; the older prose and
open questions in STORY_PACING_V3_STEADY_GOVERNOR.md are evidence, not authority.

ORDER — one reviewed/tagged release at a time; do not bundle:

FP1 v0.57.0 — PERSISTENT PER-BEDROOM NIGHT SILENCE.
- Add restored switches for main_bedroom + gabriroom, default ON.
- Only selected rooms enter #2b; changing a switch during Notte takes effect now.
- Every participating room gets a complete morning/toggle/fail-safe hand-back:
  manuale OFF, guard setpoint restored, fan explicitly alive when silence left it OFF.
- Deploy + verify FIRST; only then snapshot and strip the old Buonanotte/Sveglia
  CLIMATE branches and duplicate calls. Preserve unrelated light/cover actions.
- Replace old dashboard buttons with the two persistent switches. Delete rollback
  artifacts only after one clean week.

FP2 v0.58.0 — KNX TEMP FRESHNESS.
- Age sources from State.last_reported; keep the 30-min threshold and fallback.
- Pin flat-but-cyclic usable, genuinely dead stale, unavailable fallback.

FP3 v0.59.0 — RACK GUARD.
- Engage >28 C / 3 min; release <27 C / 10 min; start 67%.
- Escalate 100% at >=30 C / 3 min OR 20 min without >=0.3 C improvement.
- Mandatory P1 setpoint nudge opens the shared valve; guard forces BLOCCO RELEASE.
- Release restores setpoint/manuale and leaves the fan physically ON; never fan 0.
- Default-ON restored switch, alert if yielded/ineffective, fail-safe snapshot restore.

FP4 v0.59.1 — HARDENING.
- Wire MODEL_W_EDGE_SKIP=3 after chilled-water edges.
- Blend learned k only while abc is currently identified; retain stored k.
- Inject failures through every async_fail_safe stage and prove remaining releases run.
- Pin Ruff + pyproject Python 3.14 config; no mass formatting.

THEN V0 + R0-R5 STEADY GOVERNOR exactly as the implementation plan specifies.
Only living_room may pace. The living room releases safely to AUTO during Notte.
Kitchen EP is rate-of-change only (+0.4 C/10 min -> +10%, no down-step for 30 min).
The governor adapts its objective: lowest steady airflow while other rooms already own
the PdC call; reduced marginal consenso runtime when living_room owns the call. Normal
optimized fan is capped below 100%; 100% is safety escalation only.

F4c FREEZE — explicit owner decision:
- keep switch.unified_planner OFF;
- do not implement the old planner-offset ITEM 4;
- do not change planner schedule/simulation/cache/activation in this train; the only
  allowed seam edit is mechanical removal of deleted F4b from shared center composition;
- when retiring F3 live actuation, retain any pure helpers/dormant advisory structures
  imported by F4c. F4c gets its own later compatibility + shadow session.

DOCS per release: update CLAUDE.md/AGENTS.md, MASTER_PLAN and the household manual for
owner-visible behavior. Add the verified AUTO-fan fact (cycling room follows valve;
constant 100% only while valve pinned) when the relevant release lands.

RULES unchanged: pytest + ruff green on the pinned target
(pytest-homeassistant-custom-component==0.13.324 = HA 2026.4.3 / Py 3.14); commit +
tag + gh release per increment; pre-tag adversarial review; fail-safe SHA-pin
protocol; HA connector read-only unless the owner asks (ITEM 1 is owner-approved
write work). Known quirks: ~40 s KNX blips nightly ~03:00 (gateway restart).
```

## FAN-PACING MAJOR REWORK — #3 v3 "Steady Governor"

Evidence and rejected alternatives live in `STORY_PACING_V3_STEADY_GOVERNOR.md`;
the executable authority is `IMPLEMENTATION_PLAN_FIX_PACK_PACING_V3.md`.

Locked result: only living_room may pace. It holds one nonzero steady Salotto+Cucina
fan percentage while the KNX valve regulates at the honestly displayed composed
target. It releases both fans alive to AUTO during Notte. The kitchen EP contributes
rate-of-change only. The objective adapts between minimum living airflow while other
rooms already own the PdC call and reduced marginal consenso runtime while the living
room owns it. F4b is deleted; old band/F3 live paths retire only AFTER a shadow phase
and successful live soak. F4c stays OFF and code-frozen for a later dedicated session.

## Backlog after the fix pack (unchanged priorities)

1. PACING REWORK V0 + R0–R4 (above — the main train).
2. PER-ROOM OCCUPANCY ROSTER (#2 evolution; now also the designated home for the
   deleted F4b daytime-relax intent). Design story first.
3. Dedicated F4c compatibility + shadow session (offsets, steady-airflow simulation,
   forecast/model gates); do not enable it before then.
4. Outside-air merge design (free-cooling × windows × VMC) — after live data.
5. SURPLUS PRE-COOL FROM DEEP SETBACK / ON VACATION (owner-flagged 2026-07-24): don't
   waste PV via grid re-injection when a zone is far from comfort — spend real surplus
   to pre-cool even in Vacanza. Live evidence + design sketch appended to
   `STORY_PV_BIAS.md` ("BACKLOG ENHANCEMENT — surplus pre-cool from deep setback").
   Distinct from pv_bias, which is INERT under BP (can't create demand).
6. S_eff flag-on validation (live-ops, owner-paced) · #8 return-precond live pass ·
   split-trio owner decisions · winter items (seasonal).

Durable sources of truth: `CLAUDE.md` (verified facts) · `MASTER_PLAN.md` (build
checklist) · `STORY_PACING_V3_STEADY_GOVERNOR.md` (rework) · story docs per feature.
This file is the resume pointer + live state.
