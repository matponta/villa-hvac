# STORY — Split-AC trio (Palestra · Cantina Vini · Garage)

One-pager to lock the approach before a build session. Backlog item **#6**
(`MASTER_PLAN.md`). Status: **v0.45.0 — P0+P1+P1.1+P2 COMPLETE (2026-07-09), 1418
tests green. Cantina (wine, RH-aware) + Palestra (own comfort setpoint) automated;
Garage manual/observe-only. Deploy-dark (opt-in `switch.split_ac`).**

---

## 1. Situation (verified live 2026-07-08 + owner)

Three **standard `climate` entities**, `platform=knx` — **Daikin domestic
multi-split** (3 indoor heads on **one shared heat-pump outdoor unit**, a separate
refrigerant circuit from the villa PdC), bridged to KNX by **Zennio KLIC-DD v3**
gateways (ETS groups 9/10 "KLIC DAIKIN", one channel per room). Owner: *that unit
cannot make heat and cold at the same time.*

| Zone | Split entity | Role / today's use | Temp source | EP |
|---|---|---|---|---|
| **Cantina Vini** | `climate.aircon_cantina_vini_2` | **Red-wine long-term storage** (white wine is in a separate internal fridge). Optimize for the **wine, not human comfort**. Historically `dry` in winter (humidity); **now too warm for red wine** → needs real cooling. Runs `dry@22` 8×/day via `automation.circolazione_aria_cantina_vini` (master `…master_circolazione_aria_cantina`). | split `current_temperature` (25°C); **no `clima_*` twin, no thermostat** | EP `a8c934` — flappy, warm-biased ~+3.5°C |
| **Palestra** | `climate.aircon_palestra_2` (`split_climate`) | The split is palestra's **only cooler**. Its radiant is **heat-only** (see below). Used as occasional manual summer cool + a manual 30-min `dry`. | `sensor.clima_palestra` 27.4 + EP | EP `3febdc` — **works** (temp+occ) |
| **Garage** | `climate.aircon_garage_2` | Lowest priority — barely used (one manual `cool@23` burst/7d). Intent TBD (§6). | split `current_temperature` (stale when off); **no twin** | EP `3febe8` "Garage Grande" — works, **verify room** |

Caps (all three): `hvac_modes [auto,cool,dry,fan_only,heat,off]`,
`fan_modes [off,low,medium,high]`, swing on/off, setpoint **18–35** @0.1,
`supported_features=425` (**no `preset_mode`**).

**Key facts (with the corrections folded in).**
- **Palestra radiant = HEAT ONLY.** The KNX radiant loop there cannot cool — that
  is *why* a split exists in the room. So `climate.palestra_termostato_2` reading
  `cool`/`hvac_action: cooling` in summer is **cosmetic** (no chilled water to that
  floor) and must be **ignored**. ⇒ **No dual-emitter cooling fight**: radiant owns
  winter heat, split owns summer cool — non-overlapping by physics.
- **Same-mode confirmed (not a mixing VRF).** One shared heat pump ⇒ heat XOR cool
  across the trio; the integration must enforce it (no compressor entity exists in HA).
- **`dry` + `cool` coexist — documentary-confirmed (not a KNX artifact).** Daikin's
  multi-split rule: *"cooling / dry / fan can be used simultaneously; heating cannot"*
  — a head requesting the opposite direction is forced to **standby**, priority to the
  first/priority unit ([Daikin FAQ](https://www.daikin.eu/en_us/faq/my-unit-does-not-work-anymore-conflict-in-operation-modes.html) + operation manuals). The **Zennio
  KLIC-DD v3** manual encodes the same grouping: its *Simplified Mode (Status)* returns
  **0 for Cooling OR Dry, 1 for Heating** (fan not reflected). So Cool+Dry are one
  refrigerant direction and genuinely run together (matching the live 2026-07-04 ~11:15
  Cantina-`dry`+Palestra/Garage-`cool` observation); `fan_only` needs no compressor
  (always allowed). **`heat` is the only conflicting mode**; 0 heat calls in 7d → a
  conflict is a *latent winter* case only (§4/§6).
- **Where the bus CAN lie (the real risk): a heat↔cool conflict.** The KLIC-DD is a
  **single-unit** gateway with **no mode-conflict / standby object**, and this ETS
  project maps none (only `Allarme SONDA`). When the outdoor forces a head to standby,
  `[AC] Mode (Status)` keeps echoing the *requested* mode — HA would show e.g. `heat`
  while that head does nothing. ⇒ never trust a head's reported mode as proof it is
  conditioning **across the heat/cool boundary**; we must own the single-direction
  invariant (C1). Also: an **IR remote outranks KNX** at the gateway → reinforces C8.
- **Cantina physical limit:** the split's **18°C setpoint floor cannot reach the
  ~13°C ideal red-wine cellar temp**. Achievable goal = *cap the summer heat + hold
  the lowest stable temp the unit allows (≈18°C) + manage humidity*, not true cellar
  conditions. Flag to owner (§6): ideal storage would need a sub-18°C cellar unit.

## 2. Development status

Integration **v0.44.0** (repo) / **v0.44.0** (live, deployed 2026-07-08). Splits are **defined but
DORMANT**: `ZONES` carries `split_climate`/`ac_group="split_trio"`/`emitter`, but
**nothing reads them** — `PRESET_CONTROLLABLE_EMITTERS=("fancoil","radiant")`
excludes `split_ac` and every lever (#2/#10/band/fan/duty) is gated on
`emitter=="fancoil"`. The cooling stack (`CoolingController`, consenso/BLOCCO/duty)
`_is_cooling_leader` already **excludes** split zones ⇒ this work is **fully disjoint**
from the in-flight Tier-1 fold (P3) + S_eff train and can proceed in parallel.

## 3. Constraints (hard invariants)

- **C1 — Single group mode** *(owner-confirmed + Daikin/KLIC-DD documented)*. Emit
  **exactly ONE** compressor direction to the trio per cycle. `dry` is cool-side
  (compatible with `cool`); `fan_only` is compressor-independent (a head may always fan
  without engaging the group). Only `heat` vs `cool`/`dry` conflicts — never send both.
  Because the gateway has no standby/conflict feedback, a reported mode across the
  heat/cool boundary is **not trustworthy** — enforce this invariant ourselves; don't
  wait for the bus to flag a conflict (it won't).
- **C2 — Wine protection (top priority)**. Cantina is **excluded from #2 house-mode,
  #8 return-precond, occupancy and Vacanza entirely** (explicit in code) — a warm-set
  during a long absence would cook the wine. It **wins group-mode selection**. Its
  **fail-safe = self-regulating `cool` @ storage setpoint** (a split with mode+setpoint
  is its own thermostat), never `off`. Hard high-limit guard.
- **C3 — Separate circuit.** Splits never touch consenso/BLOCCO/duty/CoolingController
  (already excluded — keep it).
- **C4 — Real compressor.** We are now the only gate on this heat pump:
  **group-level** anti-short-cycle (industry-standard defaults — min-off ≈3 min, min-on
  ≈5 min, mode-change lockout ≈10 min, ≤ ~6 starts/h; configurable). Prefer setpoint
  hysteresis over on/off toggling, so start events stay rare.
- **C5 — Palestra: split=summer cool, radiant=winter heat** (non-overlapping). Split
  off in the heating season; ignore the radiant thermostat's cosmetic summer `cool`.
- **C6 — Deploy-dark + fail-safe in the same PR.** Opt-in `switch.split_ac` (default
  OFF) on top of `switch.supervisor`; fail-safe / `async_unload` / startup-resync
  branches ship **with** any actuation.
- **C7 — Non-atomic writes.** mode+setpoint+fan are separate calls, one lever/cycle:
  order **mode → setpoint → fan**; don't write setpoint/fan before the group mode is
  confirmed present.
- **C8 — Manual override couples the group.** A hand change (wall/IR) on any head is a
  manual override of the **whole group mode**; concede the trio, don't fight one lever.
  KNX re-assert-N discipline applies (telegram loss ≠ hand change).

## 4. Approach — `SplitGroupController`

New **stateful merge controller** (added to `controllers=(CoolingController, night)`
in `__init__.py`; **disjoint lever set** ⇒ merge order immaterial). Each cycle:

Automated scope = **Cantina + Palestra only**. **Garage is NOT automated** (owner
triggers it by hand) → the controller **never writes garage** and only *observes* its
mode for conflict detection.

1. **Per-room need** `{mode, setpoint, fan}` from a role:
   - *Cantina* (priority): wine storage — `cool` @ **19 °C** (owner-set) when warm,
     `dry` for humidity; **cool-side year-round**, **never heat**; ignores
     house-mode/occupancy/Vacanza.
   - *Palestra*: summer only — `cool` on occupancy (EP `3febdc`); **off in the heating
     season** (radiant heats); follows Via/Vacanza → off.
   - *Garage*: **observe-only** — never commanded by us.
2. **Group direction (C1):** the controller only ever emits **cool-side** (cantina
   cool/dry + palestra summer cool) — so **we can never create a heat↔cool conflict**.
   A conflict can only arrive from a **manual garage `heat`**; we detect it (the bus
   won't flag it), keep asserting cantina cool, and **notify** — physical protection of
   the wine relies on the installer setting **Cantina as the outdoor's priority unit**
   (see real-world check below).
3. **Group anti-short-cycle** (C4) on "any head calling".
4. **Emit levers** mode→setpoint→fan (C7).
5. **Manual override** (C8): post-reassert divergence on a commanded head (cantina/
   palestra), or any manual head `heat`, → concede + surface on `sensor.hvac_levers`.
6. **Fail-safe** (C2): cantina → `cool` @ 19 °C (self-regulating); palestra → `off`;
   garage **untouched** (not ours).

### Seam (minimal, additive)
- **arbiter.py**: new lever helpers `hvac_mode:` / `fan_mode:` (+ opt `swing:`).
  `temperature:` **reused verbatim**. `reconcile`/`merge_desired`/`values_match`
  unchanged (string levers already supported).
- **engine.py `_read_current`**: `hvac_mode` → `s.state` (mode is the entity *state*);
  `fan_mode` → `ATTR_FAN_MODE`; `swing` → `ATTR_SWING_MODE`.
- **engine.py `_dispatch_write`**: `+ set_hvac_mode / set_fan_mode / set_swing_mode`
  (`set_hvac_mode('off')` subsumes turn_on/off in one reconcilable lever).
- **model.py / build_house_state**: `ZoneSnapshot` split fields + read the split entity
  (`split_climate` for palestra, else `climate` when `emitter=="split_ac"`) + a group
  helper.
- **async_fail_safe**: dedicated split loop (no preset → NOT via `_restore_presets`);
  assert baseline on boot/master-off/unload too.
- **entities**: `switch.split_ac` opt-in + a cantina target `number` (default 19 °C);
  `sensor.hvac_split` diagnostic (group direction, per-head state, manual-heat conflict
  + override flags).
- **housekeeping**: fix stale palestra EP map (`3febdc`, for P2 occupancy);
  `PRESET_CONTROLLABLE_EMITTERS` unchanged.

## 5. Phasing (small, testable, deploy-dark first)

- **P0 — Observe. ✅ BUILT** (branch `feat/split-trio-p0`, +309 loc, 1406 tests green,
  ruff clean; NOT yet committed/version-bumped). Plumbed: `hvac_mode:`/`fan_mode:`
  arbiter lever kinds (read=state / dispatch=set_hvac_mode+set_fan_mode; `temperature:`
  reused) · `ZoneSnapshot` split fields + `split_members`/`split_mode_conflict` pure
  helpers · `build_house_state` reads each head · `engine.split_view` →
  `sensor.hvac_split` (live direction/`conflict`/per-head, computed every cycle even
  dark) · `switch.split_ac` opt-in (default off, inert) · palestra EP `3febdc` fixed.
  **No actuation** (no controller wired yet). `tests/test_split_trio.py` (7 tests).
- **P1 — Cantina wine + Palestra comfort + safety guard. ✅ BUILT** (branch
  `feat/split-trio-p0`, 1414 tests green, ruff clean; NOT committed/bumped).
  `SplitGroupController` (opt-in `switch.split_ac`, wired as a controller, disjoint
  levers): **Cantina** self-regulating `cool@19 °C` (fan low, cool-side year-round,
  home/away/season-agnostic — the dead-man) · **Palestra** `cool` only in
  summer+home+occupied (EP `3febdc`) else `off` · **Garage** never commanded
  (observe-only). Pure core `split_head_target` + `split_dwell` (per-head
  anti-short-cycle C4). `_split_fail_safe` hands back ONLY managed heads (cantina→
  cool@19 dead-man, palestra→off; deploy-dark unload never touches an unmanaged
  split). Cantina setpoint tunable via `OPT_SPLIT_CANTINA_SETPOINT` (default 19).
  **P1.1 humidity handling** (the split can only DEHUMIDIFY): the storage head is
  RH-aware via `sensor.everything_presence_one_a8c934_humidity` (EP; stale→temp-only
  fallback) — `dry` above `OPT_SPLIT_RH_CEILING` (65 %), setpoint relaxed
  `+SPLIT_DRY_SETPOINT_RELAX` (1.5 °C) below `OPT_SPLIT_RH_FLOOR` (55 %) to avoid
  over-drying, plain `cool@19` in-band. **Caveat: a too-DRY cellar needs a humidifier
  (out of scope)** — the AC defends only the ceiling. Cantina reads ~44 % live (dry
  side) so the floor-relax matters. `sensor.hvac_split` now shows per-head RH.
  18 tests in `tests/test_split_trio.py`. **P2 folded in** (palestra comfort here).
  > ⚠️ **DEPLOY GATE:** before flipping `switch.split_ac` on, DISABLE the legacy
  > `automation.circolazione_aria_cantina_vini` (+ `…master_circolazione_aria_cantina`)
  > — it sets cantina to `dry` on a timer and would fight the controller (the arbiter
  > would read the pulse as a manual override and concede for 2 h). The controller now
  > owns humidity (P1.1), so retiring it is safe — it's the same function, done on RH.

## 6. Owner decisions — ALL RESOLVED (2026-07-08)

1. **Cantina setpoint = 19 °C** (owner). Keep `dry` available for humidity; explicit RH
   target deferred (secondary). Accepted that the ideal ~13 °C is **not reachable** with
   this unit — 19 °C is the working compromise (a sub-18 °C cellar unit would be a
   separate future purchase, out of scope).
2. **Autonomously running the shared heat pump — OK** (owner accepts).
3. **Garage — NOT automated** (owner triggers manually). ⇒ controller emits cool-side
   only and never creates a conflict; garage is observe-only.
4. **Garage intent — manual only** (resolved by #3).
5. **Compressor protection — industry-standard defaults**, configurable: min-off ≈ 3 min,
   min-on ≈ 5 min, mode-change lockout ≈ 10 min, ≤ ~6 starts/h. Setpoint hysteresis
   preferred over on/off toggling, so start events are rare anyway.

**Real-world check (non-blocking, recommended):** confirm/ask the installer to set
**Cantina as the priority indoor unit** on the Daikin outdoor, so a manual garage `heat`
can never steal cooling from the wine (it would be blocked instead). Software detects +
notifies, but the priority setting is the physical guarantee.

**Status: high-level plan APPROVED — building P0.**

---

### Equipment & sources
- **Units:** Daikin domestic multi-split (3 indoor heads, 1 shared outdoor heat pump)
  via **Zennio KLIC-DD v3** KNX gateways. ETS: `../knx/GroupAddressesReport_2026-03-12`
  groups 9 (`KLIC DAIKIN 01` = on-off/mode/%/swing) + 10 (`KLIC DAIKIN 02` =
  setpoint/auto-man/temp/alarm), one channel per room (1 Palestra · 2 Cantina · 3 Garage).
- **Mode-compatibility (dry↔cool):** [Daikin FAQ — operation-mode conflict](https://www.daikin.eu/en_us/faq/my-unit-does-not-work-anymore-conflict-in-operation-modes.html)
  (cool/dry/fan compatible, heat exclusive, non-priority head → standby) + Zennio
  KLIC-DD v3 manual *Simplified Mode (Status)* (Cooling **or** Dry = 0, Heating = 1).
- **Live corroboration:** 2026-07-04 ~11:15 Cantina `dry` + Palestra/Garage `cool`,
  no fault (HA recorder).
