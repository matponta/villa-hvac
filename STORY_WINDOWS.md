# STORY — Window contacts (owner ask 2026-07-11)

Owner installed Shelly BLU Door/Window contacts (BTHome/bluetooth) in 6 rooms
and asked for three behaviors:

1. **Window open → stop the AC in that room.**
2. **> X windows open AND outside cooler than inside → assume free cooling.**
3. **A single window open for a long time → alert Mattia and Ehi.**

## Discovered entities (2026-07-11, device-area verified)

| Contact | Device name | Area | ZONES mapping |
|---|---|---|---|
| `binary_sensor.main_bedroom_finestra_piccola_bedroom_window` | Finestra piccola bedroom | main_bedroom | `main_bedroom` |
| `binary_sensor.gabri_room_finestra_g_window` | Finestra G | gabri_room | `gabriroom` |
| `binary_sensor.aaa_window` | Finestra V | studio_v | `studio_v` |
| `binary_sensor.shelly_blu_door_window_9756_window` | (unnamed, 9756) | office | `office` |
| `binary_sensor.entrance_porta_vetri_ingresso_window` | Porta vetri ingresso | entrance | `ingresso` (radiant) |
| `binary_sensor.shelly_blu_door_window_b50c_window` | Porta Cucina | kitchen | **`living_room`** (see below) |

NOT wired: `binary_sensor.cantina_impianti_porta_cantina_window` (plant-room
door, not in the ask — was OPEN during discovery, so wiring it would have
paused a zone on day one). Device class `window`, states on=open / off=closed —
already in `WINDOW_OPEN_STATES`/`WINDOW_CLOSED_STATES`, so the #4
`WindowController` consumes them unchanged. BTHome contacts are battery/BLE:
`unavailable` is ignored by the controller (a dead battery never pauses or
un-pauses a room) and counts as CLOSED for rule 2 (never a false free-cool).

**Kitchen → living_room:** the kitchen has NO thermostat (open space, follows
Salotto; `climate: None`), so a `kitchen` pause would be inert — the preset
lever lives on the Salotto thermostat which drives BOTH valves. The open space
is one air volume: with the kitchen door open the Salotto AC fights it too.
The Porta Cucina contact therefore pauses the `living_room` leader (Salotto +
Cucina). Owner-visible: opening the kitchen door pauses the salotto too.

## Part 1 (v0.55.0): per-room pause (rule 1)

- `window:` keys added to the 6 zones above; mechanism = existing #4
  (1-min debounce → building_protection → restore to house mode on close;
  paused zones skipped by #2a/band/precool/#2b-nudge already).
- Closes the known #4 edge: the #2b heat-guard no longer runs the FAN in a
  window-paused bedroom (guard held in silence while `z.paused` — "stop AC in
  that room" includes the fan). Golden updated deliberately.
- `ingresso` is radiant: no summer cooling to pause; the key matters in winter
  (heating pause) + rules 2/3.

## Part 2 (v0.55.0): windows → free cooling (rule 2)

- Opt-in `switch.windows_free_cooling` (unique suffix windows_free_cool; default OFF, restore) — same discipline as
  `switch.free_cooling` (v0.53.0: owner wants explicit control of auto-coast).
- Pure predicate on the cycle snapshot: `open contacts ≥ OPT_WINDOWS_FREE_COOL_COUNT`
  (default 3, options) AND summer AND `outdoor ≤ house_indoor − OPT_WINDOWS_FREE_COOL_MARGIN`
  (default 1.0 °C; house_indoor = mean fused temp of the cooled leaders).
  Count = binary_sensor contacts only (the 3 bathroom vasistas covers are NOT
  "airing the house" signals).
- Wired as an OR into `_is_free_cooling` (and policies.py `_free_cooling` now DELEGATES to it — the duplicated predicate would have let presets and band diverge) → the ENTIRE existing free-cool stack
  follows for free: BP on cooled zones, band yields, #2b guard fan-only,
  house_mode skips, plan regime `free_cool`, fail-safe restore of BP presets.
- Threshold-only (no hysteresis) — same accepted trade-off as #5 (outdoor and
  indoor temps move slowly; the count is discrete).

## Part 3 (v0.55.0): long-open alert (rule 3)

- Per-contact timer in `window.py`: open ≥ `OPT_WINDOW_ALERT_MINUTES`
  (default 30, 0 disables) → one notification per opening episode to
  `notify.mobile_app_matphone16` + `notify.mobile_app_pixel_10`
  (options-overridable, comma-separated) — resolved from person.mattia_pontacolone
  / person.ehi device trackers. Reset on close; suppressed while the house is
  DELIBERATELY airing (windows-free-cool active or `switch.free_air` on).
  Alerts year-round (an open window wastes heating in winter too).

## Pre-tag adversarial review (29 agents, 2026-07-11): outcomes

FIXED before tagging:
- **Verdict entry dwell** (MAJOR): the raw airing conditions must hold
  `WINDOWS_FREE_COOL_DWELL` (5 min) before the house-wide coast engages — a
  30 s kitchen-door transit could otherwise slam every zone to BP and cycle
  the compressor. Exit stays immediate (safe direction).
- **Suppression re-arms, never consumes** (MAJOR): a page suppressed while
  deliberately airing reschedules itself — so the cleaning-day
  "five closed, one forgotten" window still pages once the count drops.
  Count-based suppression is also SUMMER-gated (the windows_free_cooling
  switch left on in January must not silence winter heating-waste alerts).
- **Truthful page** (MAJOR): "il clima della stanza è in pausa" is claimed only
  when the pause is actually engaged (zone paused + master + Auto setback);
  otherwise the page warns "il clima NON è in pausa".
- Unavailable-at-fire re-arms (BLE blip must not drop the episode); Porta
  Cucina page names the door (not "Salotto"); the #2b guard fan is now 0 during
  ANY free-cool coast (matches the paused behavior — no stirring warm air
  against a shut valve); indoor_mean skips #10-disabled leaders; flap/startup/
  engine-level-coast/feature-graph tests added.

ACCEPTED / deferred (documented, follow-ups in NEXT_SESSION):
- `window_pause_policy` stays gated on Auto setback (pre-existing #4
  semantics): with setback OFF rule 1 does not actuate — the page now warns
  honestly. Exempting the pause from the gate needs a restore-path redesign
  (with setback off nothing re-asserts the house preset on close).
- Default-config cleaning-day = up to 6 pages/phone over an airing: enabling
  `switch.windows_free_cooling` (rule 2) or `free_air` suppresses them; a
  grouped/digest notification is a possible refinement.
- A habitually-open bedroom window during Notte pages nightly (~30 min after
  bedtime): as-specified rule 3; a quiet-hours / bedroom-during-Notte deferral
  is an owner decision.
- Close→restore has no debounce (a wind-flapping window cycles BP/comfort
  ~90 s): watch live; add a close debounce if observed.

## Later / explicitly out of scope now

- Per-room "Open windows" manual switches (backlog #4 item): the contacts cover
  the 6 main rooms; the global `free_air` switch stays as the manual fallback
  for sensor-less rooms. Rename deferred.
- Full outside-air merge (free-cooling × windows × VMC): rule 2 is its first
  concrete piece; revisit after live data.
- `binary_sensor.up_sense_contact` (mystery legacy contact) still unmapped.
