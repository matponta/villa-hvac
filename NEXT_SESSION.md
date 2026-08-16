# Next session — kickstart prompts

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
