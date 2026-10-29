# STORY — Palestra + Bagno Palestra damp control (brief for Claude Code, 2026-10-29)

Owner request (Mattia, 2026-10-29): *"il deumidificatore del bagno palestra e lo split
palestra possono regolare l'umidità di quei due locali, che sono comunicanti (porta
abitualmente aperta). Molto muro vecchio contro terra + una doccia: dobbiamo gestire
l'umidità."* Constraint from the owner: **leave the VMCs out of this story**.

Read `CLAUDE.md`, `STORY_SPLIT_TRIO.md` and `supervisor/cellar.py` first — this story
re-uses the split-trio seam (levers, dwell, fail-safe, deploy-dark) and the cellar
pattern (RH-aware head + plain `power:` lever on a Shelly plug).

---

## 1. Facts found live (2026-10-29 — verified with HA tools, don't re-derive)

### Rooms & coupling
- Palestra and Bagno Palestra are separate KNX zones but ONE air volume in practice:
  the door between them is habitually open (**no contact sensor on it**).
- `binary_sensor.up_sense_contact` ("PortaPalestra") is the **palestra → stairs** door,
  NOT the bagno door. Owner: kept "quasi sempre socchiusa" (open ~96 % of the last 7 days).
  ⇒ the zone leaks to the stairwell; the dehumidifier also partly dries the stairs.
  Do not use this contact as a gate.
- 1–2 walls against soil (old masonry), a shower in the bagno. Radiant = heat only.

### Actuators
| Actuator | Entity | Notes |
|---|---|---|
| Dehumidifier (bagno palestra) | `switch.shellyplugsg3_9070694d542c` (Shelly native) + power `sensor.shellyplugsg3_9070694d542c_power`, energy `…_energy` | **Continuous drain** (owner) → no tank-full case. **Same physical plug** is ALSO exposed via Matter as `switch.shelly_plug_s_gen3` ("Presa deumidificatore", MAC 90:70:69:4d:54:2c) and is paired in **Apple Home** (Matter fabric 1). Use ONLY the Shelly-native entity as lever; treat any change coming from Matter/Apple/Shelly app as a manual override. |
| Split palestra | `climate.aircon_palestra_2` (Daikin head on the shared trio outdoor unit, via Zennio KLIC-DD) | Already managed by `SplitGroupController`, role `comfort`: summer+home+occupied → `cool`, else `off`. **Humidity is not managed for palestra today.** |

- Nothing references the dehumidifier plug today (no automation/script/scene/dashboard).
- Legacy `automation.30_min_deumidificatore_palestra`: OFF, no trigger, sets the split to
  `dry` 30 min then off. Superseded by this story → **delete at deploy** (see §6).

### Sensors — the three gym hygrometers DISAGREE
Live 29/10 22:50:

| Sensor | Entity | RH | T used | Dew point |
|---|---|---|---|---|
| KNX palestra (wall thermostat, GA 8/0/2) | `sensor.umidita_palestra` | 63.6 % | 21.7 (`sensor.palestra_temperature`) | 14.5 °C |
| KNX bagno palestra (GA 8/0/3) | `sensor.umidita_bagno_palestra` | 62.3 % | 21.6 | 14.1 °C |
| UniFi UP Sense on the stairs door | `sensor.up_sense_humidity` | 67 % | 22.1 | 15.7 °C |
| EP1 3febdc | `sensor.everything_presence_one_3febdc_humidity` | **48.7 %** | 23.2 (offset −1.5 already) | 11.8–13.2 °C |
| Outdoor (gw3000a) | `sensor.gw3000a_dewpoint` | — | 6.9 | 6.3 °C |

- KNX and UP Sense track each other (weekly means 60–80 %); **EP1 reads ~15–25 points
  lower all year**. Assessment (confidence HIGH): the EP1 under-reads RH because it
  self-heats (temperature offset −1.5 °C set, humidity offset 0). Which of KNX/UP is the
  true value is unknown (confidence MEDIUM) — no reference hygrometer.
- ⇒ **Control on the KNX pair. Never on the EP1.** (The palestra `ZoneSnapshot` has no
  `humidity_sensor` today — `sensor.hvac_split` shows `humidity: null` for palestra.)
- `sensor.umidita_bagno_palestra` IS a working RH sensor (1 000 changes / 5 days, ~0.1–0.2 %
  steps, a few short `unavailable` blips). `const.py` comments claiming bagno palestra has
  no working RH sensor are wrong.

### History (long-term statistics, from Nov 2025)
- Palestra KNX weekly mean: Dec–Feb 43–55 %; **May → Oct 70–82 %** (worst Jun–Sep, outdoor
  dew point 16–18 °C). Bagno: 55–72 %.
- **Natural experiment 12–16/10/2026**: dehumidifier ran ~24 h/day (5.7–6.1 kWh/day ≈ 250 W
  mean) → bagno 72 → 50–51 %, palestra 80 → 58–61 %, UP Sense 80 → 62 %. Switched off on
  16/10 → back to 70–80 % within ~2 days. ⇒ **the bathroom dehumidifier alone, door open,
  is enough for both rooms.** Who switched it on/off is unknown (outside the 7-day recorder).
- Outdoor dew point since 20/10 ≈ 5–7 °C vs indoor 12–15 °C: the moisture is generated
  indoors (soil walls + shower), not imported.
- 29/10 ~07:00 both KNX RH dropped ~69 → 61 % in an hour while palestra T rose
  (20.2 → 21.7 °C, heating season). RH is temperature-confounded: **log dew point / AH
  alongside RH** before attributing a change to the dehumidifier.
- Shower signature in `sensor.umidita_bagno_palestra` is weak (+2–3 % within an hour, e.g.
  65.8 → 68.6). No clear shower trace identified → **no shower-boost in this story** (P2).
- Recorder history ≈ 7 days; statistics are permanent. Dehumidifier energy has stats.

### Why it matters
ASHRAE 160 mould criterion: 30-day running mean **surface** RH < 80 % (surface 5–40 °C).
Soil walls are colder than the air: air 21.7 °C / 64 % → surface RH ≈ 85 % on a 17 °C wall;
at the October mean (≈ 80 % air) even a 19 °C wall saturates. Wall temperatures are NOT
measured (assumption). EPA: keep indoor RH < 60 %, ideally 30–50 %.

---

## 2. Goal

One controller, two actuators, keep `RH_ctrl = max(umidita_palestra, umidita_bagno_palestra)`
below a ceiling year-round, at minimum energy, without ever creating a split-trio
mode conflict and without touching the VMCs.

## 3. Constraints (inherit split-trio C1–C8, plus)

- **G1 — VMC out of scope.** Do not modify `VMC_GROUPS`, `vmc.py`, `switch.vmc_auto` or
  anything VMC. (Known, separate issue: `VMC_GROUPS["ground"]["humidity_rooms"]["palestra"]`
  uses the EP1 → will never trigger. Note it in `NEXT_SESSION.md`, don't fix here.)
- **G2 — Cool-side only for the split** (C1): palestra may get `dry` or `cool`, never `heat`.
  The garage manual-heat yield (`_heat_call`, v0.88.0) still wins → split off, the
  dehumidifier carries the load.
- **G3 — Winter: split stays OFF** (C5, unchanged). `dry` cools the room and fights the radiant.
  Winter dehumidification = dehumidifier only (its ~250 W heat is a bonus).
- **G4 — Summer: split first, dehumidifier second.** The split removes heat AND water; the
  dehumidifier dumps ~250 W into a room being cooled. Dehumidifier only if the split
  can't hold the ceiling (stage 2) or is yielding/unavailable/overridden.
- **G5 — Humidity defence is home/away/Vacanza-agnostic** (like the cellar). Occupancy only
  changes the *comfort cooling* part of palestra, not damp control.
- **G6 — Deploy-dark**: new opt-in `switch.gym_damp` (default OFF) on top of
  `switch.supervisor`. Fail-safe / unload / master-off: dehumidifier → hand back
  (restore the state we found, snapshot at first actuation, like VMC ownership), split →
  existing palestra fail-safe (`off`).
- **G7 — Manual wins.** A plug change we didn't command (HA UI, Shelly app, Matter/Apple
  Home) → concede via the arbiter's existing override backoff; surface on `sensor.hvac_levers`.
- **G8 — Sensor health.** One KNX sensor stale/unavailable → use the other. Both stale >
  `GYM_RH_STALE_MIN` (30 min) → fall back to `sensor.up_sense_humidity`; that stale too →
  hold current state for 2 h, then dehumidifier OFF + alert. Never use the EP1.

## 4. Approach

### 4.1 Model / const (additive)
- `ZONES["palestra"]["humidity_sensor"] = "sensor.umidita_palestra"`,
  `ZONES["bagno_palestra"]["humidity_sensor"] = "sensor.umidita_bagno_palestra"`.
  Check every consumer of `ZoneSnapshot.humidity` first (grep) — confirm nothing else
  (VMC, window, comfort) starts behaving differently because palestra/bagno now have RH.
  If something does, read the KNX pair inside the new controller instead and leave the
  zone map alone.
- `GYM_DEHUM_SWITCH = "switch.shellyplugsg3_9070694d542c"`,
  `GYM_DEHUM_POWER = "sensor.shellyplugsg3_9070694d542c_power"`,
  `GYM_RH_FALLBACK = "sensor.up_sense_humidity"`.
- Options (options flow + `number.*`, like the split ones):
  `OPT_GYM_RH_CEILING` default **62 %**, `OPT_GYM_RH_RELEASE` default **55 %**,
  `OPT_GYM_DEHUM_MIN_ON` **30 min**, `OPT_GYM_DEHUM_MIN_OFF` **15 min**,
  `OPT_GYM_STAGE2_DELAY` **120 min** (summer: split alone above ceiling this long → add dehumidifier).
  The 62/55 band is a starting guess (target: stay under EPA's 60 % on a sensor that may
  read high) — tune after 2 weeks of data.

### 4.2 Pure core — `supervisor/gym_damp.py` (no HA imports, fully unit-tested)
`gym_damp_decision(rh_palestra, rh_bagno, rh_fallback, *, season, split_available,
split_yielding, stage2_elapsed, dehumidifying, ceiling, release) -> GymDampDecision`
returning `dehum: bool`, `split_humidity: "dry" | None`, `reason`, `rh_ctrl`, `source`.

- `rh_ctrl = max(valid KNX)`; fallback per G8.
- Hysteresis: start at `rh_ctrl ≥ ceiling`, stop at `rh_ctrl ≤ release`.
- **Winter**: `dehum = active`, `split_humidity = None`.
- **Summer**: active → `split_humidity = "dry"`; `dehum = True` only if
  `stage2_elapsed ≥ stage2_delay` OR `split_yielding` OR `not split_available`.
- Reasons (Italian, for the UI): `asciutto`, `deumidifica`, `split_dry`,
  `split_dry+deumidificatore`, `split_occupato` (split yielding to garage heat),
  `sensore_ripiego` (UP Sense), `sensori_ko`, `manuale`, `disattivo`.

### 4.3 Split integration (`split_head_target`, role `comfort`)
Extend with an optional `humidity_mode` argument from the decision:
- summer + `split_humidity == "dry"`: if home & occupied & `temp > comfort_setpoint` →
  `("cool", comfort_setpoint)` (cooling also dries); else → `("dry", None)`.
  Runs even if unoccupied/away (G5).
- winter → `("off", None)` regardless (G3).
- existing per-head dwell (`split_dwell`) and the garage heat yield apply unchanged.

### 4.4 Controller
Prefer a small `GymDampController` merged next to `SplitGroupController` (disjoint lever:
`power:switch.shellyplugsg3_9070694d542c`), with the split part fed into
`SplitGroupController` via `HouseState` (do NOT let two controllers write the palestra
split lever). Dwell on the plug via `split_dwell` with the gym min-on/min-off.
Observe-only tick while dark (keep the view/telemetry live, like `observe_dark`).

### 4.5 Telemetry & alerts
- `sensor.hvac_palestra_umidita` (state = reason) with attributes: `rh_palestra`,
  `rh_bagno`, `rh_fallback`, `rh_ctrl`, `source`, `dewpoint_palestra` (Magnus from KNX RH +
  `sensor.palestra_temperature`), `dewpoint_outdoor`, `ceiling`, `release`, `dehum_on`,
  `dehum_power`, `split_mode`, `stage2_since`, `enabled`. Give dew point a
  `state_class: measurement` companion sensor so statistics survive.
- Alerts (once per episode, same recipients as the split/window alerts):
  1. `rh_ctrl ≥ 75 %` for 6 h while active ("palestra troppo umida, non tengo");
  2. dehumidifier commanded ON but `power < GYM_DEHUM_RUN_W` for 30 min ("deumidificatore
     non lavora": own humidistat satisfied, fault, or no restart after a power cut);
  3. sensors KO (G8).
  `GYM_DEHUM_RUN_W`: measure in P0 (expect ~200–300 W running, < 5 W idle).

## 5. Phasing

- **P0 — Physical checks + observe (owner + code).**
  Owner: (a) set the dehumidifier's own humidistat to **continuous / lowest** so the plug
  is the only controller; (b) verify it **restarts by itself after a power cut** (plug off
  1 min → on → power above idle within 5 min); (c) note running/idle W; (d) optional:
  reference hygrometer next to the palestra thermostat for 24 h to settle KNX vs UP.
  Code: const/model/options, pure core + tests, `sensor.hvac_palestra_umidita` computed
  every cycle, `switch.gym_damp` inert. Ship + deploy dark, watch 48 h of the view.
- **P1 — Dehumidifier actuation (winter path).** Enable `switch.gym_damp`. This is the
  season we're in; split stays off (G3). Delete the legacy automation (§6).
- **P2 — Summer split `dry` + stage 2** (can be built now, only exercised next spring;
  tests cover it). Shower boost evaluated here only if a clear shower trace shows up.

## 6. Deploy steps (owner confirms each)
1. Version bump (current `0.89.0` → `0.90.0`), CHANGELOG, `NEXT_SESSION.md`.
2. Deploy dark; check `sensor.hvac_palestra_umidita` reads the KNX pair and the reason is sane.
3. Owner flips `switch.gym_damp` ON.
4. Delete `automation.30_min_deumidificatore_palestra` (owner OK required — HA config, not code).
5. Optional, owner decision: remove the plug from Apple Home / the Matter integration to
   cut the third control path (otherwise it just shows up as manual overrides).

## 7. Acceptance
- Unit tests for the pure core: hysteresis, both seasons, stage 2 timing, yield, each
  sensor-failure branch, EP1 never read; split target tests extended (`test_split_trio.py`):
  winter never `dry`, summer unoccupied `dry`, garage heat → off.
- Full suite green, ruff clean.
- Live, after P1 (7 days): daily mean `rh_ctrl` ≤ 60 %, no hour > 70 % except right after
  a shower, dehumidifier energy ≤ ~6 kWh/day (expect well under thanks to the hysteresis),
  ≤ 6 plug starts/h, no split-trio conflict, no fight with a manual change.

## 8. Open questions for the owner (don't block P0)
- Dehumidifier model → operating temperature range (low-temp frost protection) and
  whether its compressor has a built-in restart delay.
- Keep the plug in Apple Home?
- Target band 62/55 OK, or stricter (e.g. 58/52) accepting more kWh?

---

## As built — v0.90.0 (2026-10-29, P0 + P1 + P2 code; deploy-dark)

- **Pure core** `supervisor/gym_damp.py`: `gym_rh_source` (max of the KNX pair →
  one KNX → UP Sense → none; the EP1 is not an input), `gym_damp_decision`
  (hysteresis 62/55, winter = dehumidifier only, summer = split `dry` first +
  stage 2 / yield / split-unavailable → dehumidifier, blind hold 2 h then OFF),
  `dew_point` (Magnus).
- **Zones**: `humidity_sensor` = `sensor.umidita_palestra` / `sensor.umidita_bagno_palestra`.
  Only consumers of `ZoneSnapshot.humidity`: the cantina head (cantina only), the
  split view, and `split_head_target` (comfort role ignores `rh`) → no behaviour
  change. The damp law reads the pair separately with a 30-min `last_reported`
  freshness check (`_fresh_num`), so a silent KNX sensor counts as stale.
- **`GymDampController`** (policies.py) owns ONLY `power:switch.shellyplugsg3_9070694d542c`
  (dwell = gym min-on/min-off via `split_dwell`), runs BEFORE `SplitGroupController`
  and publishes `decision`; the split controller (single writer of the palestra
  head) reads `decision.split_humidity` → `split_head_target(humidity_mode="dry")`:
  summer `dry` whoever is home, `cool` if occupied and warm; winter always `off`.
  Split "available" = `switch.split_ac` on + head not unavailable; with
  `split_ac` off the summer law goes straight to the dehumidifier.
- **Hand-back**: plug state snapshotted at the first write; restored by
  `async_fail_safe` (`_gym_fail_safe`, pin updated) and by turning
  `switch.gym_damp` off (`engine.async_release_gym`). In-memory like the #9
  stint: a crash (no unload) leaves the plug as last commanded.
- **Manual wins**: a plug change we didn't command is conceded by the arbiter
  (2 h backoff); `sensor.hvac_palestra_umidita` then reads `manuale`.
- **Entities**: `switch.gym_damp` (default OFF), `switch.gym_damp_alerts`
  (default ON), `sensor.hvac_palestra_umidita` (reason + attributes; `disattivo`
  while dark, the would-be reason in `wanted_reason`),
  `sensor.palestra_umidita_di_controllo` + `sensor.palestra_punto_di_rugiada`
  (MEASUREMENT), numbers `number.palestra_umidita_massima` / `…_di_rilascio`,
  `number.deumidificatore_palestra_minimo_acceso` / `…_spento`,
  `number.palestra_attesa_deumidificatore_in_estate` (no options-flow fields:
  knobs are numbers, like the cellar's).
- **Alerts** (`damp.py`): ≥75 % for 6 h, plug ON but < `GYM_DEHUM_RUN_W` (50 W,
  provisional until P0 measures it) for 30 min, every hygrometer out for 2 h.
- **Known limits**: a manual override of the palestra split is not seen by the
  damp law (it escalates to the dehumidifier only after stage 2); the stale
  "bagno palestra has no RH sensor" comment the brief mentions was not found in
  `const.py`.
- Tests: `tests/test_gym_damp.py` (27). Suite 914 green, ruff clean.
