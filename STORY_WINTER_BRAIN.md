# STORY — Winter brain (radiant floor)

Owner ask 2026-10-07, after the changeover to INVERNO (v0.79.0 live: winter
light + fancoils OFF). The owner wants, working ONLY on the thermostat
setpoints (never the radiant valves):

1. **How long does it take to bring the house to temperature** from a setback or
   with the plant off ("tempo per portare in temperatura").
2. **How low can I go** while away when I come back e.g. for the weekend
   ("quanto posso abbassare").
3. **Heating from photovoltaic** — bank PV surplus as heat in chosen rooms
   (bedroom + other selected zones) even while away.
4. The other anticipatory items: pre-heat before the return / wake, sun-aware.

## Signals (live 2026-10-07)

- 15 radiant valve STATES created by the owner as KNX binary_sensors
  (`binary_sensor.radiante_<x>_valvola`, read-only for us — the true per-room
  heating demand, same role the EV FAN valves play in summer). Map:
  zona_giorno→living_room · camera_padronale→main_bedroom ·
  camera_gabriele→gabriroom · camera_ospiti→studio_v · studio→office ·
  sala_giochi · palestra · ingresso · pianerottolo_p1→stairs_p1 · lavanderia ·
  bagno_gabriele · bagno_p1→bagno_giochi · bagno_pt→bagno_ingresso ·
  bagno_palestra · bagno_padronale→bagno_padronale_01 AND _02 (shared).
  pianerottolo_p2 has NO valve sensor.
- `binary_sensor.ct_consenso_caldo_villa` (plant heating call), outdoor
  `gw3000a`, solar GHI + per-facade S_eff, fused room temps (thermostat-primary).
- Condominio PV: battery SoC `sensor.battery_percentage_2`, signed grid power
  (negative = export), PV remaining today (see memory condominio-pv-energy-map).

## Model (per room, valve-driven, deliberately simple)

    valve CLOSED (after the slab residual):  dT/dt = −a·(T − T_out)        (+ sun)
    valve OPEN   (after the dead time lag):   dT/dt =  k_h − a·(T − T_out)  (+ sun)

- `a` [1/h] envelope loss incl. slab coupling, learned on long valve-closed,
  low-sun (night) windows after `RESIDUAL` (90 min: the slab keeps emitting).
- `k_h` [°C/h] radiant heating rate with the valve held open, learned on
  valve-open low-sun windows after the lag.
- `lag` [min] dead time from valve-open to a +0.15 °C rise, per episode.
- Priors (until learned): a 0.03/h, k_h 0.6 °C/h, lag 45 min. Each parameter
  carries a sample count; the confidence is n/(n+N0).
- Recovery time to target (closed form): T∞ = T_out + k_h/a;
  t = lag + ln((T∞ − T_now)/(T∞ − T_target)) / a; None if T∞ ≤ target.
- Coast-down (valve closed) toward a setback s: T(t) = T_out + (T0 − T_out)·e^(−a·t),
  floored at s (the thermostat holds it).

Summer and winter models are SEPARATE: the summer `k` is a fancoil cooling
capacity in minutes; the radiant floor responds in hours.

## Releases

| Phase | Content | Actuates? |
|---|---|---|
| **W1 v0.80.0** | Valve map (`heat_valve` in ZONES) + `WinterModel` observer (a, k_h, lag; own Store) + per-room `sensor.<room>_inverno` (recovery minutes to the comfort target, params, confidence) + house `sensor.tempo_riscaldamento` (time until the whole house is at temperature, now). Winter-only learning. | no |
| W2 v0.81.0 | **Setback advisor** ("quanto posso abbassare"): for a return ETA (the #8 date + daypart entities) compute the deepest away setpoint ≥ floor so that the coast + recovery fits, the pre-heat start time and the degree-hours saved; sensor + dashboard. | no |
| W3 v0.82.0 | **Winter return pre-conditioning**: #8 re-enabled in winter on the winter model — Via+armed holds the advised setback, then ramps to comfort at the computed start. Opt-in. | setpoints |
| W4 v0.83.0 | **PV heating**: per-room `switch.<room>_pv_heat`; on real Condominio surplus (export / battery full, dwell + hysteresis) raise those rooms' setpoint by `pv_heat_boost` (cap) — also in Via/Vacanza (lifting BP → comfort for those rooms only). The slab is the thermal battery. Opt-in. | setpoints |
| W5 | Wake pre-heat / sun-aware evening (tomorrow sunny → lower tonight), F4c planner hook. | setpoints |

Guardrails: setpoint-only (never valves); every winter write stays inside
[15, 25]; manual override wins (arbiter); fail-safe unchanged; advisory before
actuation; every actuating feature opt-in and summer-inert.
