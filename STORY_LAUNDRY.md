# STORY — Laundry drying through the lavanderia vasistas (v0.97.0, 2026-11-26)

Owner (Mattia, 2026-11-26): *"il vasistas della lavanderia è rimasto aperto tutta
la notte, non mi piace. Il clima era fermo, la stanza fredda. Credo ieri la
signora abbia steso lì, ma così la roba non asciuga e la casa si raffredda."*
Then: *"non voglio due fasi. Facciamolo una volta, bene."*

## 1. What happened (verified live 2026-11-26)

- The vasistas was driven by HA automations, not by hand:
  - `automation.vasistas_lavanderia_a_c_3` (ON): opens when
    `sensor.variazione_umidita_lavanderia_30m` > 6 for 5 min and starts
    `timer.vasistas_lavanderia` (40 min); closes when the variation is < 2 for
    10 min **and** the timer is idle.
  - `automation.close_laundry_vasistas_if_open_too_long` (ON): closes after 8 h open.
  - `automation.vasistas_lavanderia_a_c` / `_a_c_2`: already OFF (older versions).
- **The bug.** The close trigger fires once, when the variation settles. An open
  vasistas drops the RH within ~5 min, so the variation settles while the
  40-min timer still runs → the condition fails → the close is dropped and
  never retried. 25/11: opened 23:47:18, closed 07:47:18 by the 8 h safety.
- **The oscillation.** With wet laundry, every close raised the RH by 6–12
  points in 15 min (50 → 59–63 %) → re-open. 25/11: opened 11:08, 11:42,
  14:12, 21:17, 23:47.
- **The heating.** The #4 window pause put `climate.lavanderia_termostato_2` in
  building_protection (setpoint 7 °C) at 23:48; the radiant stayed off all
  night with 6.5 °C outside. Room 20.0 → 19.4 °C (floor mass).
- Air: outdoor 7.2 °C / 96 % ≈ 7.6 g/m³; room 19.4 °C / 50 % ≈ 8.3 g/m³ —
  outside is barely drier than a room with no laundry; with laundry
  (20 °C / 55 % ≈ 9.5 g/m³) a purge removes water.

## 2. The rule (as built)

Drying needs **warm air + short exchanges**: open a few minutes while the
laundry has raised the room, close, keep heating, repeat. Never one long opening.

Pure core `supervisor/laundry.py` (`laundry_step`, `LaundryParams`):

| | cold (outdoor < 15 °C or unknown) | mild |
|---|---|---|
| purge starts at room RH | ≥ 55 % | ≥ 55 % |
| …only if outside is drier by | ≥ 1.0 g/m³ | ≥ 1.0 g/m³ |
| purge length | 5 min min, **15 min max** | 5 min min, 60 min max |
| rest between purges | 20 min | 10 min |
| an opening by hand is closed after | **60 min** (+ phone page) | 8 h |

A purge ends early at RH ≤ 50 % (after 5 min), when outside stops being drier
(< 0.5 g/m³ left), on rain (gw3000a ≥ 0.1 mm/h — closes at once), or with lost
sensors. No purge while raining or with the room under 17 °C.

HA wrapper `laundry.py` (`LaundryController`), edge-triggered off the
coordinator tick, OUTSIDE the arbiter (like VMC and winter sun):

- writes `cover.open_cover` / `close_cover` only on its own decision; a command
  in flight is waited (90 s grace), a close that never lands is re-sent;
- **heating stays on during our purges**: `WindowController._pause` asks
  `laundry.owns_purge("lavanderia")` and skips the building_protection while it
  is our purge (re-checked every minute; a purge past its cap loses the
  exemption). An opening by hand pauses the room as before;
- bookkeeping persisted (Store `villa_hvac_laundry`): a restart mid-purge still
  closes on time;
- `switch.laundry_drying` (default ON, restored) on top of the master. OFF
  closes a purge of ours at once, then hands off — a manual opening is never
  touched while OFF;
- HA stop and entry unload close a purge of ours (`async_release`);
- `sensor.lavanderia_asciugatura`: state = chiuso / pausa / ricambio /
  aperto_a_mano / fuori_umido / pioggia / stanza_fredda / sensori_ko /
  vasistas_ignoto / spento; attributes: room/outdoor temp + RH + absolute
  humidity, open_since, max_open_min, next_purge_at, last_close_reason,
  purges_today.

Inputs: `sensor.umidita_lavanderia` + `sensor.clima_lavanderia` (KNX thermostat,
fallback to the climate's attributes; stale after 30 min), `sensor.gw3000a_
outdoor_temperature`, `sensor.gw3000a_humidity`, `sensor.gw3000a_rain_rate`.

## 3. Deploy gate (HA side)

1. HACS download v0.97.0 + restart.
2. **Disable** `automation.vasistas_lavanderia_a_c_3` and
   `automation.close_laundry_vasistas_if_open_too_long` (otherwise a/c 3 keeps
   opening and our controller reads it as a hand opening). The two older
   `a_c`/`a_c_2`, `timer.vasistas_lavanderia` and
   `sensor.variazione_umidita_lavanderia_30m` can be deleted later.
3. Check `sensor.lavanderia_asciugatura` reads `chiuso` (or `ricambio` if laundry
   is hung) and `switch.laundry_drying` is ON.

## 4. Open / not done

- Thresholds are constants (no knobs): tune after the first laundry days from
  the sensor's attributes (purges_today, the RH curve).
- No dehumidifier in the lavanderia; VMC 1 does not serve it (not in
  `VMC_GROUPS.humidity_rooms`).
- Summer behaviour is the same law with mild timings; not yet observed live.
