# STORY — VMC year-round (v0.86.0, 2026-10-28)

Both ventilation units are on the Salda **Smarty** integration since 2026-10-28
12:54: speed 33/66/100 % on `fan.*`, the unit's own outdoor/supply/extract air
temperatures, filter timer (a TIMESTAMP), alarm, warning, boost switch.

| Unit | Serves | Job |
|---|---|---|
| VMC 1 "Bagno Cantine" (`fan.10_5_150_27`) | cantina vini, palestra, bagno ingresso, bagno palestra — 1-2 walls against soil | **fight damp** |
| VMC 2 "Cucina e Casa" (`fan.vmc_cucina_e_casa`) | kitchen, master suite + bath, office, studio V | comfort, quiet at night |

## Facts found live (2026-10-28)

- Both ran 33 % all day; ~half of the recorded states were `unavailable` 2 s
  blips → every reading is held for 10 min (`hold_value`).
- VMC 1's "outdoor" probe read 17.3 °C at 03:00 with the gw3000a at ~5 °C: it is
  NOT outdoor air (intake from an indoor space, or a probe in the plant room).
  Decisions always use gw3000a; `outdoor_probe_trusted=false` on its sensors.
- VMC 2 at night: supply 18.4 > extract 18.1 with outdoor 7.5 → efficiency
  > 100 %: a post-heater or a mis-read probe. Open question for the owner.
- Both filter timers were already due (warning on).
- The Smarty entities have no `state_class` → no long-term statistics.

## What it does

All decisions compare **absolute humidity** (g/m³): ventilation moves water, and
90 % RH outside at 8 °C is drier than a 50 % RH room at 20 °C.

1. **Drying (VMC 1, any season).** A room at/above its RH ceiling (palestra 65,
   cantina = the split's wine band ceiling, `split_rh_ceiling`) AND outdoor AH
   ≤ room AH − 1 g/m³ → 66 % (100 % at ≥ 75 % RH). Release at ceiling − 5 % or
   when the outside air stops being drier (hysteresis 0.5 g/m³). A cantina below
   its floor (`split_rh_floor`) caps it at 66 %: mould beats dry corks.
   Bagno ingresso/palestra have no working RH sensor (antibagno EP unavailable).
2. **Damp-air flush veto (VMC 1, summer).** When the outside air holds more
   water than the dampest room, the #5 night boost is vetoed: warm humid air on
   cold earth walls condenses.
3. **Quiet flush (VMC 2, summer).** The occupied-Notte veto used to cancel the
   flush outright; it now runs at 66 % instead.
4. **Winter boost cap.** A boost we did not command, on for 60 min in winter, is
   switched off (heat loss). The bathroom button (15 min) and the script (30 min)
   are untouched; the ground unit keeps a boost while it is drying.
5. **Telemetry (always).** Per unit: `… stato` (why — `base`, `deumidifica`,
   `deumidifica_forte`, `cantina_secca`, `umido_fuori`, `lavaggio_silenzioso`,
   `non_attivo`, with the whole picture as attributes), `… recupero calore`
   (efficiency %, inferred bypass), and the three air temperatures as
   MEASUREMENT sensors so statistics survive.
6. **Alerts** (`switch.vmc_alerts`, default ON, independent of the master),
   once per episode to the window-alert recipients: filter due, alarm 10 min,
   offline 30 min, unit OFF 1 h, a ground room ≥ 75 % RH for 2 h while the
   outside air is too wet to dry it.

## Ownership rules (the speed lever)

- Raised only for a reason; nothing ever LOWERS the owner's speed. Snapshot the
  live speed before raising, restore it when the reason ends (20 min min-hold).
- A speed changed by hand while we hold it (2 reads, after a 2 min write grace)
  or a unit switched OFF → conceded, never written back.
- Ownership persisted in `.storage/villa_hvac_vmc`: after a restart a raise
  still at our value is adopted (then kept or handed back); one changed since is
  forgotten. HA stop / unload / `vmc_auto` off hand everything back.

Actuation needs `switch.supervisor` + `switch.vmc_auto` (deploy-dark).

## Open / to verify live

- `fan.set_percentage` 66 on Smarty: does the unit hold it, or does its own
  weekly program put it back? (If it does, the concede rule drops ownership.)
- Where VMC 1 takes its air from; whether VMC 2 has a post-heater.
- Thresholds are constants (`VMC_DEHUM_*`, `VMC_ALERT_*` in const.py); move to
  the options flow once a season of data says which ones need tuning.
