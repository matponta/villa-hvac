# Next session — kickstart prompts

## Local candidate ready — v0.64.0 (2026-07-15)

The full approved fix-pack + steady-governor scope is implemented locally. Tests and
Ruff are green. Next work is deployment/live validation: install v0.64.0, merge
`dashboard_v0.64.0_cards.yaml`, keep `paced_living_room` OFF while observing shadow,
then enable it after owner acceptance. `unified_planner` remains OFF. Legacy
Buonanotte cleanup still requires a live HA snapshot before deletion.

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
5. S_eff flag-on validation (live-ops, owner-paced) · #8 return-precond live pass ·
   split-trio owner decisions · winter items (seasonal).

Durable sources of truth: `CLAUDE.md` (verified facts) · `MASTER_PLAN.md` (build
checklist) · `STORY_PACING_V3_STEADY_GOVERNOR.md` (rework) · story docs per feature.
This file is the resume pointer + live state.
