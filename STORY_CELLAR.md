# STORY — Wine cellar year-round (v0.87.0, 2026-10-28)

The cantina vini keeps red wine for years (whites are in a separate fridge).
Tools: the Daikin split head (`climate.aircon_cantina_vini_2`, cool-side only,
floor **18 °C**, one outdoor unit shared with palestra + garage), a circulation
fan on a Shelly plug (`fan.wine_cellar_shelly_plug_cantina`), and VMC 1's
supply/extract. Two walls are against the earth.

## Facts found live (2026-10-27/28)

- **Stagnant air lies.** Fan OFF: the EP reads ~23.5 °C / 42 %. Within 10 min of
  the fan going ON: ~19.5 °C / 50 %. Same water content (~8.7 g/m³) — the fan
  doesn't change the air, it brings the real air to the sensor. The old "EP is
  +3.5 °C warm-biased" note was this pocket, not a sensor offset.
- So the cellar is really **~19.5 °C / ~50 %**: a bit warm, a bit dry.
- Three legacy automations fought the split every 1–2 h
  (`circolazione_aria_cantina_vini`: `dry` 1 h then `off`;
  `master_circolazione_aria_cantina`: `off` at 00:01; `solo_ventilatore_cantina`:
  fan 1 h at 03/07/11/14/17/19/21/23). With `switch.split_ac` on, each pulse was
  undone a minute later.

## The window

Wine wants 12–14 °C and 60–70 % RH, and above all stability. The unit can't go
below 18 °C, so:

| | Summer | Winter |
|---|---|---|
| split setpoint | 18 °C (`split_cantina_setpoint`) | 18 °C (`cellar_winter_setpoint`) — trims only |
| temperature OK | 16–20 °C | 12–19 °C (the cellar cools by itself) |
| humidity OK | 55–70 % | 55–70 % |
| too dry (<55 %) | setpoint +1.5 (max 20) | setpoint +1.0 (max 19) |
| too damp (>70 %) | `dry` (or `cool` if warm) until 67 % | same |

Nothing heats: a `heat` call would fight the shared outdoor unit. Below the
low edge we only warn. Temperature beats humidity: the relax never leaves the
window.

## What it does

1. **Circulation fan** (`power:` lever): the first 20 min of every hour, and
   continuously while drying.
2. **Mixed reading**: a temperature/RH pair counts only once the fan has run
   8 min; it is held until the next run and expires after 90 min. With no
   reading the head simply holds `cool` at the setpoint on its own probe.
3. **Head**: hold / dry / relax with 3 % hysteresis (`supervisor/cellar.py`).
4. **VMC 1** now judges the cantina on the mixed reading only (the raw value
   looked dry and capped the ground unit's drying for nothing).
5. **Sensors**: `sensor.cantina_vini_stato` (ok / calda / fredda / umida /
   secca / sconosciuta + window, mode, reason), `…_temperatura`, `…_umidita`
   (MEASUREMENT, mixed air).
6. **Alerts** (`switch.cellar_alerts`, default ON, independent of the master):
   warm 3 h, cold 6 h, RH > 75 % for 6 h, RH < 50 % for 24 h, no mixed reading
   for 3 h while we drive the fan.

Actuation needs `switch.supervisor` + `switch.split_ac`.

> ⚠️ **Deploy gate:** disable the three legacy automations above, or they keep
> overriding the split and the fan (the arbiter would concede for 2 h each time).

## Open

- A cellar that stays below 55 % needs a humidifier: the split can only dry.
- Thresholds are options without a UI yet (`split_cantina_setpoint`,
  `cellar_winter_setpoint`, `split_rh_floor`, `split_rh_ceiling`).
