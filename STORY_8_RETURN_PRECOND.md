# Story #8 — Rientro programmato & pre-condizionamento (build spec, v0.25.0)

Code-grounded design (verified against engine.py / policies.py / supervisor.py on
2026-07-01). Supersedes the generic "weekend scenes" framing of #8.

## User intent (confirmed 2026-07-01)
Quando esci dichiari **quando torni** (data + fascia grossolana). La casa va in
**building_protection** (setback profondo, PdC ferma) e riparte **in anticipo** il
tanto che basta per essere a comfort al tuo arrivo. Simmetrico estate/inverno.

Decisioni utente:
- **Formato ETA:** grossolano — `date` + fascia `mattino/pomeriggio/sera`
  (→ ora canonica configurabile; default 08/14/19).
- **All'arrivo:** tieni comfort e **aspetta la presenza** (#2c flippa a Casa); nessun
  auto-switch all'ETA, nessun re-setback se tardi.
- **Come chiede:** push azionabile (`notify.mobile_app_*`) all'ingresso in Via +
  modulo dashboard.
- **Setback in attesa:** `building_protection` (antigelo).

## Architettura — override della MODALITÀ EFFETTIVA (non leve dirette)
`FanBandController`, `precool_policy`, `house_mode_policy` calcolano tutto da
`state.house_mode` + `state.mode_offset` (→ `center`). Quindi #8 **non emette leve**:
sovrascrive la modalità effettiva mentre `house_mode == Via` **e** armato:
- **In attesa** (ora < ETA − lead_time): effective mode = **Vacanza**
  → `house_mode_policy` mette `building_protection` su tutte le zone controllabili;
  `mode_offset = None` → `FanBandController` non è eligible → rilascia i fancoil in
  AUTO; `precool_policy` inerte. Casa quiescente, PdC ferma.
- **Finestra pre-cond** (ora ≥ ETA − lead_time): effective mode = **Casa**
  → comfort preset + setpoint = `house_setpoint`; il band controller (se
  `fan_pacing`) pace al center comfort. Resta Casa anche oltre l'ETA (hold & wait).
- **house_mode ≠ Via** o non armato o opt-in off → #8 inerte (comportamento Via
  normale, +5).

Zero conflitti di leva: si riusa l'intero stack esistente.

## Componenti
### Entità nuove (component-owned)
- `switch.villa_hvac_return_precond` — **opt-in** (deploy-dark gate, come
  duty_cycle/fan_pacing). Default off.
- `switch.villa_hvac_return_armed` — un rientro è impostato (lo settano notifica/
  dashboard; "Non so" → off = solo BP profondo, niente rampa).
- `date.villa_hvac_return_date` — data di rientro (nuova Platform.DATE).
- `select.villa_hvac_return_daypart` — `mattino/pomeriggio/sera`.
- `sensor.villa_hvac_return_plan` — diagnostica per il modulo dashboard: stato
  (waiting/precond/off), ETA risolta, "tra Xh", ora inizio pre-cond, per-stanza
  "pronta all'arrivo?" (dalle room_trajectories).

### Opzioni (config_flow)
`OPT_RETURN_DAYPART_HOURS` (8/14/19), `OPT_RETURN_MAX_LEAD` (h, clamp del lead,
default 6), `OPT_RETURN_MARGIN` (min di margine, default 30), `OPT_NOTIFY_TARGET`
(servizio notify.mobile_app_*; auto-discover se assente).

### Logica pura (supervisor.py, unit-testabile)
- `return_eta(date, daypart, daypart_hours, now) -> datetime | None`.
- `return_lead_time(state, *, max_lead, margin) -> timedelta` — per stanza raffrescata:
  ΔT = max(0, temp − comfort_target); rate netto = k − a·(T_out−target) − b·S − c
  (floor piccolo > 0); tempo = ΔT/rate; **max fra le stanze**, + margine, clamp
  [15min, max_lead]. Usa il modello blended (model_a..k) o i prior → advisory finché
  k non converge (coerente con stanze gain-limited: rate→0 ⇒ clamp a max_lead ⇒
  parte il prima possibile, comfort all'arrivo non garantito per le stanze dure).
- `return_effective_mode(house_mode, armed, opt_in, eta, lead_time, now, latched)
  -> (effective_mode | None, latched)` — con **latch**: appena si entra nella
  finestra si resta (evita chatter quando il lead_time si accorcia raffrescando).

### Controller stateful `AwayReturnController` (returnhome.py)
Tiene il latch. Il motore lo chiama in `_cycle` **dopo** `build_house_state` e
**prima** delle policy: `state = self.away_return.apply(state, hass, entry,
thermal, commit=actuate)` → ritorna uno `state` con `house_mode`/`mode_offset`
sovrascritti (via `dataclasses.replace`). `commit=actuate` così il latch avanza solo
quando si attua (come i timer di duty/pacing); deploy-dark calcola read-only per la
plan view. Il vero `house_mode` select resta "Via" (fonte utente); l'override è
interno all'attuazione.

### Trigger "quando torni"
Listener sulla transizione `select.house_mode → Via` (manuale o auto-#2c). Se opt-in
on e non già armato: `notify.<target>` azionabile con azioni **[Stasera][Domani
mattino][Domani sera][Scegli…][Non so]**. Il component ascolta
`mobile_app_notification_action` → scrive armed/date/daypart. Guardrail: **una sola
volta** per transizione in Via.

### Presenza (#2c)
Rientro anticipato → #2c porta il select a Casa → #8 inerte, latch reset. L'ETA è un
upper bound, non un vincolo.

### Dashboard
Modulo in CoolClima (nuova sezione in Overview o tab dedicato): stato rientro,
ETA + "tra Xh", ora inizio pre-cond, per-stanza pronta/non pronta, controlli
armed/date/daypart + toggle opt-in.

### Deploy-dark / fail-safe / stagione
Opt-in + master. Nessuna leva nuova ⇒ il fail-safe globale (rilascia BLOCCO +
manuale→AUTO) copre già #8 (l'override sparisce all'unload → torna Via nativo).
Estate live; inverno segue il flag `winter` (condivide l'incertezza #7 sul caldo).

### Test
`test_returnhome.py`: `return_eta` (fasce/rollover giorno), `return_lead_time`
(stanza facile vs gain-limited→clamp), `return_effective_mode` + **latch** (no
chatter al confine), gate (non-Via/non-armato/opt-in off → None), interazione con
disabled/paused (l'override non forza zone #10/#4, che restano BP). Estendere
`test_engine.py` per l'apply nel ciclo; `test_config_flow.py` per le nuove opzioni.

### Release
`v0.25.0` (commit + tag + gh release), CI verde + test.
