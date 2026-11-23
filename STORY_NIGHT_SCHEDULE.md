# STORY — Fascia notturna del clima + preriscaldo sveglia ("Notte" staccata dal clima)

Stato: **implementata in v0.96.0 (23/11/2026), non deployata.** Scritta in chat il 23/11/2026 (sessione HA, non
Claude Code). Base di codice letta: v0.95.0 (b272dd3). Da implementare qui.

## Decisioni di Mattia (23/11/2026)
1. **"Notte" esce dal clima.** `select.house_mode` diventa Casa / Via / Vacanza.
   Di notte la modalità NON cambia.
2. **Fascia notturna a orario** in Casa: un offset notturno su tutta la casa, senza
   cambio di stato ("così il resto della casa risparmia"). **Default 21:30 → 07:00**,
   orari modificabili da HA.
3. **Preriscaldo = parte del clima quando c'è una sveglia impostata**
   (`input_boolean.wakeup_active` on + `input_datetime.wake_up_time`): la fascia
   finisce prima, così alla sveglia le stanze sono a comfort.
4. **Il segnale "si dorme" è un helper HA separato:** `input_boolean.notte`
   (creato in chat il 23/11). Lo accende Buonanotte (`automation.spegni_tutto_e_chiudi`,
   anche dal telecomando camera), lo spegne Sveglia (`automation.sveglia_in_casa`,
   anche da Smart Wakeup), e `automation.notte_spegnimento_automatico` lo spegne
   su Via/Vacanza e alle 12:00. Già migrate a questo helper: luce Wardrobe e
   blocco boost VMC doccia. **villa_hvac deve usarlo** per #2b (camere silenziose)
   e per il night-quiet della VMC.

**Assunzioni fatte in chat (Mattia non ha risposto — confermare prima di chiudere):**
- anticipo del preriscaldo **dal modello invernale per stanza, tetto 3 h**
  (fallback 3 h fisse se il modello non è affidabile);
- in estate stesso meccanismo (vedi "Aperto" per l'orario d'inizio estivo).

## Perché (dati, camera padronale)
- Notte 22–23/11: Notte alle 22:14 (setpoint 21→20). Temperatura ferma 21,1–21,2
  fino a ~01:30 (residuo del massetto ~3 h), 20,5 alle 06:30, minimo 20,1 alle
  07:13 DOPO il ritorno a Casa, 20,4 alle 09:00. Valvola mai aperta.
- Modello W1: a 0,0117/h (τ≈85 h), k_h 0,419 °C/h, lag 45 min, conf 0,84.
  Recupero stimato: +0,5 °C ≈ 2 h 50, +1 °C ≈ 5 h (con ~5 °C fuori).
- Quindi: (a) la fascia deve **iniziare presto** (il massetto scalda ancora 3 h);
  (b) deve **finire prima della sveglia** di un anticipo che dipende da stanza e
  meteo; (c) con il pulsante Notte la mattina restava 0,5–0,9 °C sotto.
- Risparmio atteso piccolo (~1 %, ordine di grandezza; Mattia informato). Il
  valore è soprattutto comfort al risveglio + niente stato da ricordare.

## Design

### Entità nuove (component-owned, stato ripristinato al riavvio)
- `switch.fascia_notturna` — opt-in (convenzione deploy-dark), default **off**;
  al deploy Mattia lo accende (vedi checklist).
- `time.fascia_notturna_inizio` (default 21:30) e `time.fascia_notturna_fine`
  (default 07:00). Se in estate serve un orario diverso → coppia `_estate`
  (vedi "Aperto").
- `sensor.fascia_notturna` — stato `spenta` / `attiva` / `preriscaldo` /
  `inattiva` (motivo); attributi: inizio, fine effettiva, sveglia usata (sì/no,
  ora), anticipo e inizio preriscaldo **per stanza**, offset applicato, stagione.
- Offset: **riusare** le opzioni esistenti `winter_notte_offset` (−1, scelta
  Mattia 7/10) e `summer_notte_offset` (+3), solo rietichettate "Offset fascia
  notturna" nel config flow (chiavi invariate = nessuna migrazione).
- Opzioni nuove: entità sveglia (default `input_datetime.wake_up_time`,
  `input_boolean.wakeup_active`), helper sonno (default `input_boolean.notte`),
  `preheat_max_lead` (default 3 h).

### Logica (ogni ciclo dell'engine)
- Agisce **solo con house_mode = Casa** (Via/Vacanza/#8/W3/W4 restano padroni dei
  loro setpoint) e con `switch.fascia_notturna` + Auto setback on.
- Finestra che attraversa la mezzanotte: attiva se `inizio ≤ ora < fine_effettiva`.
- **Fine senza sveglia:** `fine` fissa per tutte le stanze.
- **Con sveglia attiva:** prossima occorrenza di `wake_up_time` dopo l'inizio della
  finestra corrente; per ogni stanza `fine_stanza = sveglia − anticipo_stanza`.
  - Inverno: `anticipo_stanza` = tempo di recupero W1 da (target − |offset|) a
    target con T esterna prevista fino alla sveglia (stesso calcolo di
    `sensor.<stanza>_inverno` / W2), **clamp [0, preheat_max_lead]**; se
    confidenza < 0,5 o stanza "irraggiungibile" (T∞ ≤ target) → `preheat_max_lead`.
    (Ricordare la lezione W3 del 22/10: senza clamp i lead esplodono — Bagno
    Ingresso a=0,0071.)
  - Estate: fancoil veloci → anticipo dal modello estivo se pronto, altrimenti
    30 min fissi.
  - Sveglia impostata quando il preriscaldo dovrebbe già essere partito → la
    stanza esce subito dalla fascia (nessun salto).
- Sveglia disattivata a metà notte → si torna alla `fine` fissa.

### Suggerimento d'implementazione
- La fascia è un **trim per zona**, non una modalità: sommarlo all'offset di zona
  in `build_house_state` (es. campo `z.night_trim` sommato dove oggi si usa
  `house_setpoint + mode_offset + z.setpoint_offset`), con `mode_offset` = 0 in
  Casa. Così inverno (`winter.py`), band center, precool, planner lo vedono senza
  rami nuovi. Verificare che W4 PV heat (che confronta mode vs casa) resti
  inerte in Casa come oggi.
- **Arbiter:** i cambi di setpoint ai bordi della fascia e per-stanza al
  preriscaldo devono essere trattati come un cambio di modalità (re-assert
  legittimo), NON come override manuale — vedi v0.94.0 ("no phantom overrides on
  mode change"). Test dedicato sull'eco KNX.

### Rimozione di Notte dal clima
- `HOUSE_MODES` = Casa/Via/Vacanza; `MODE_PRESET` senza Notte. Ripristino del
  select: valore salvato "Notte" → Casa.
- `controller.mode_offset`: via il ramo Notte.
- `away.escalation_target` e `AlarmAwayController` (v0.95.0): solo Casa → Via.
- #2b `night_active` (engine.py ~722): **estate AND helper sonno on AND Auto
  setback AND non svegliato**. `night.py`: il latch di risveglio si azzera quando
  l'helper va off (oggi: mode ≠ Notte); l'auto-wake usa la sveglia HA se attiva,
  altrimenti `auto_wake_time`.
- `vmc.py` night-quiet: helper sonno on (oggi mode == Notte).
- Helper non disponibile → trattato come **off** (nessun silenzio, nessun veto VMC).
- Aggiornare `supervisor/planner.py` (riga "night"), config flow, strings/
  translations, CLAUDE.md/MASTER_PLAN, manuale.

### Test minimi
Finestra a cavallo della mezzanotte; riavvio a metà finestra; sveglia off/on a metà
notte; sveglia impostata dopo l'inizio del preriscaldo; clamp e fallback del lead;
stanza irraggiungibile; precedenza Via/Vacanza/#8; nessun override fantasma
dell'arbiter ai bordi; #2b e VMC guidati dall'helper (anche unavailable);
ripristino select da "Notte"; escalation away senza Notte; passaggio d'ora legale.

## Fase C — checklist lato HA (sessione chat, la sera del deploy, prima delle 21:30)
1. Deploy + restart; accendere `switch.fascia_notturna`; controllare
   `sensor.fascia_notturna` (inizio/fine, sveglia, lead per stanza).
2. `automation.spegni_tutto_e_chiudi` (Buonanotte): `input_select.modalita_casa`
   → **Casa** invece di Notte (Buonanotte = siete a casa); l'helper resta.
3. `automation.telecomando_hue_bedroom` ramo on_press: togliere il
   `select_option Notte` (lo fa già Buonanotte).
4. `input_select.modalita_casa`: togliere l'opzione Notte; verificare i due bridge
   (`clima_bridge_modalita_casa_supervisor_one_way` / `..._ritorno`).
5. `automation.gabriele_a_casa_camera_e_scaldasalviette`: `["Casa","Notte"]` → Casa.
6. Automazioni legacy DISABILITATE che citano Notte (`clima_applica_modalita_casa`,
   `notte_guardia_caldo_camera_*`, `notte_sveglia_automatica_camere`,
   `clima_backup_via_quando_esco`, `clima_rientro_in_casa_ripristina_fancoil`,
   `clima_risincronizza_modalita_all_avvio`): chiedere a Mattia se eliminarle.
7. Dashboard: tile `select.house_mode` (opzioni dall'entità), card CoolClima per
   `switch.fascia_notturna` + 2 orari + sensore.
8. Notte successiva: verificare sul campo (padronale: valvola, temperatura alla
   sveglia vs target).

Fino al deploy: Buonanotte imposta ancora `modalita_casa = Notte` (−1 inverno),
quindi il clima si comporta come oggi.

## Aperto
- **Inizio estivo:** con i fancoil la stanza risponde in minuti; 21:30 + 3 °C in
  estate scalda la zona giorno mentre siete ancora svegli. Proposta: coppia di
  orari estiva (default 23:30 → 07:00). Da chiedere a Mattia prima dell'estate.
- Conferma delle due assunzioni sopra (lead dal modello con tetto 3 h; estate).
