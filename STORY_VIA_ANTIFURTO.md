# STORY_VIA_ANTIFURTO — Via automatico dall'antifurto, regola per stagione (target v0.95.0)

Brief del 23/11/2026 (sessione chat, proposta "Clima e stanze", punto 6). Owner: Mattia.
Prerequisito: v0.94.0 live (fix arbiter: senza, un Via automatico partirebbe con 2 h di ritardo).

## Perché (dati 16–23/11/2026)
- Via/Casa cambiati a mano dall'app 4 volte in 7 giorni; Via messo 2–3 h dopo l'uscita
  (18/11, 23/11); il 22/11 la casa è rimasta in Casa per 8,7 h di assenza.
- Inverno = radiante: il modello invernale stima perdite di ~0,1–0,3 °C/h a casa ferma,
  quindi un setback di giornata vale ~1–3 % del giorno e costa una ripresa lenta al
  rientro. NON vale la pena per le uscite brevi. Estate = fancoil veloci: il setback rende.
- L'escalation di oggi (`away.py`, #2c) usa solo i tracker `person.*`, ritardo 18 h, e il
  timer è in memoria: riparte a ogni riavvio (19/11 riavvio 09:52 → Via alle 03:54).
- L'antifurto è il segnale di casa vuota più affidabile: viene inserito quando esce
  l'ultima persona, Cristina compresa, e copre anche Gabriele, che non ha tracker.

## Comportamento voluto
Un **episodio** inizia quando `input_select.antifurto_ajax_ultimo_stato` (mirror persistente
dell'Ajax, opzioni Sconosciuto/Disinserito/Inserito/Inserito notte/Allarme) passa a
`Inserito`, e finisce appena il mirror ha qualunque altro valore.

Durante un episodio, porta `select.house_mode` a **Via** quando TUTTO questo è vero
all'istante dello scatto:
1. mirror == `Inserito` (non `Inserito notte`) da almeno il ritardo della stagione corrente;
2. presenza fusa `aggregate_presence(PRESENCE_PERSONS)` == not_home (None/unknown = non scattare);
3. modalità attuale in Casa/Notte (riusa `escalation_target`); Via/Vacanza: niente;
4. `switch.auto_setback` ON e il nuovo `switch.via_da_antifurto` ON.

Ritardo per stagione (`current_season(hass, entry)`, NON da s5a che è fermo dal 17/11):
- estate: **30 min** (`number.via_da_antifurto_ritardo_estate`, minuti, 0–240, passo 5)
- inverno: **8 h** (`number.via_da_antifurto_ritardo_inverno`, ore, 1–48, passo 1)
Il ritardo si misura da `armed_since`; se la stagione o i numeri cambiano a metà episodio,
ricalcola `fire_at = armed_since + ritardo`.

Regole dell'episodio:
- **Al massimo uno scatto per episodio.** Se dopo lo scatto qualcuno rimette Casa a mano
  (pre-riscaldamento per il rientro, come il 19/11 20:42), non riportare Via.
- **Se `select.house_mode` cambia per mano di altri prima dello scatto, annulla l'episodio**:
  la scelta manuale vince. Distingui i cambi tuoi da quelli altrui (contesto o flag interno).
- **Il rientro resta com'è**: `AwayController._restore` riporta Casa quando la presenza torna
  `home`. NON ripristinare su `Disinserito`: l'app Ajax disinserisce da programmazione alle
  07:30 nei giorni di Cristina (lun/mer/ven) e la casa deve restare in Via.
- **L'escalation 18 h su soli tracker resta** come rete di sicurezza se ci si dimentica di
  inserire l'antifurto. Non toccarla in questa story (nota aperta sotto).

Notifica "Quando torni?" (`returnhome.py`, parte già all'ingresso in Via): verifica che parta
anche per il Via automatico. **Novità:** se lo scatto avviene tra le 22:00 e le 07:30,
rimanda la notifica alle 07:30 (configurabile come opzione, default 22:00–07:30).
D'inverno con 8 h di ritardo lo scatto cade spesso di notte.

## Robustezza
- **Persisti `armed_since`** in uno `Store` (stile `villa_hvac_season`): un riavvio non deve
  far ripartire il conteggio. Al boot: mirror `Inserito` + valore salvato → riprendi con
  il tempo residuo; nessun valore salvato → parti da adesso (scelta conservativa).
  Mirror diverso da `Inserito` al boot → cancella il valore salvato.
- Niente `async_call_later` come unica fonte di verità: pianifica con
  `async_track_point_in_time` su `fire_at`, ripianifica al boot e al cambio di
  stagione/numeri, e ricontrolla le condizioni allo scatto.
- Usa solo il mirror, non `alarm_control_panel.ajax_antifurto` (torna `unknown` a ogni
  riavvio finché Ajax non manda un evento). Il mirror può essere stantio se HA era giù
  durante un evento: per questo serve anche la presenza not_home.
- `Allarme` o `Sconosciuto` chiudono l'episodio, senza scatto.

## Entità nuove (seguire lo schema di `switch.gym_damp` e dei `CellarNumber`)
- `switch.via_da_antifurto`: EntityCategory.CONFIG, RestoreEntity, **default OFF**
  (deploy-dark, lo accende Mattia).
- `number.via_da_antifurto_ritardo_estate` (min) e `number.via_da_antifurto_ritardo_inverno` (h):
  RestoreEntity, default 30 e 8.
- `sensor.via_da_antifurto`, diagnostico. Stato: `spento` / `in_attesa` / `scattato` /
  `annullato` / `inattivo`. Attributi: `armed_since`, `fire_at`, `season`, `delay`,
  `reason` (perché non scatta: presenza a casa, modalità manuale, ecc.). Mattia vuole poter
  fare debug senza leggere il codice.
- Costanti in `const.py`: `ALARM_MIRROR = "input_select.antifurto_ajax_ultimo_stato"`,
  `ALARM_ARMED = "Inserito"`, default dei ritardi e della fascia silenziosa.
- Niente template, niente automazioni HA: tutto dentro villa_hvac.

## Dove
- `away.py`: logica pura testabile, per esempio
  `alarm_away_due(mirror, armed_since, now, season, delays, presence, mode, enabled) -> (bool, reason)`,
  più un controller (nuovo `AlarmAwayController` o estensione di `AwayController`) che
  ascolta mirror, presenza e `select.house_mode` e chiama lo stesso `_set_mode`.
- `returnhome.py`: rinvio della notifica nella fascia silenziosa.
- `switch.py`, `number.py`, `sensor.py`, `__init__.py`, `strings.json` / `translations/`.

## Test (pytest-homeassistant-custom-component 0.13.324, py3.14)
Puri:
- inverno: Inserito + via da 7h59 → no; 8h00 → sì; estate: 30 min → sì;
- Inserito notte / Disinserito / Sconosciuto / Allarme → no;
- presenza home o None → no (`reason` corretta);
- modalità Via/Vacanza → no; switch o auto_setback OFF → no;
- cambio stagione a metà episodio → nuovo `fire_at`.

Con HA (freezer):
- scatta una sola volta; Casa manuale dopo lo scatto → nessun nuovo Via nello stesso episodio;
- cambio manuale di modalità prima dello scatto → episodio annullato;
- riavvio a metà episodio (Store) → stesso `fire_at`;
- Disinserito durante Via → nessun ripristino; presenza home → ripristino Casa (comportamento esistente);
- scatto alle 03:00 → notifica "Quando torni?" rimandata alle 07:30; scatto alle 15:00 → subito;
- la vecchia escalation 18 h continua a funzionare con il nuovo switch OFF.
Suite completa verde. `tests/test_window.py::test_page_claims_pause_only_when_actually_engaged`
è instabile anche sulla v0.93.1: rilanciarlo se è l'unico a fallire.

## Rilascio
v0.95.0: ruff, test, commit, tag, `git push origin main v0.95.0`, `gh release create v0.95.0`.
Aggiornare `NEXT_SESSION.md` e `CLAUDE.md` (sezione away/#2c). Dopo il deploy Mattia accende
`switch.via_da_antifurto`.

Verifica dal vivo:
- uscita con antifurto inserito → `sensor.via_da_antifurto` = `in_attesa` con `fire_at` giusto;
- d'inverno un'uscita sotto le 8 h non tocca la modalità; un'assenza più lunga porta Via,
  e la notifica arriva subito o alle 07:30;
- un riavvio di HA durante l'attesa non sposta `fire_at`.

## Fuori da questa story (lato HA, non repo)
- **Smart Wakeup a casa vuota.** `automation.smart_wakeup` lancia `automation.sveglia_in_casa`
  con `skip_condition`, e la condizione di presenza è disabilitata. Se `wakeup_active`
  resta acceso durante un'assenza, all'ora della sveglia `script.casa_apri` sblocca i Nuki
  e rimette Casa. Serve una condizione nativa: mirror diverso da `Inserito`. La aggiunge la
  sessione chat, con l'OK di Mattia.
- **Card in CoolClima → Casa** (switch, 2 numeri, sensore di stato): la aggiunge la
  sessione chat dopo il deploy.
- **Aperto:** l'escalation 18 h sui soli tracker può mettere Via con Gabriele in casa da
  solo. Valutare se richiedere anche lì l'antifurto inserito. Decisione di Mattia.
