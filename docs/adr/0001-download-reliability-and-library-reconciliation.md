# ADR-0001: Download reliability, delegated-download reconciliation & library integrity

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** @sossiogll (maintainer)

## Context

Un benchmark di sincronizzazione dell'intera libreria (~3400 tracce) ha fatto emergere una
serie di problemi di affidabilità e alcune lacune nell'integrazione con i download provider
delegati (Lidarr). Problemi osservati sul campo:

1. **File `.ogg` corrotti/troncati** (83 casi): contenitore Ogg valido ma header Vorbis rotto
   (`Invalid Setup header`), durata incoerente (download incompleto). Oggi finiscono in
   libreria senza alcuna verifica → non riproducibili su Navidrome.
2. **Mislabel di contenuto**: file taggato "What I've Done" contenente in realtà l'audio di
   "Crawling". I tag non sono affidabili: l'unico modo per sapere *cos'è* un file è l'audio
   (fingerprint), non i metadati.
3. **Duplicati dal fallback album Lidarr**: quando il download della singola traccia fallisce,
   si delega a Lidarr che scarica **l'intero album**. Se alcune tracce esistono già (rip/qualità/
   naming diversi), si creano duplicati. La risoluzione (tieni qualità migliore) è stata fatta
   **manualmente** confrontando i file.
4. **Delega fire-and-forget**: `dispatch_album` solleva `DownloadDelegated` e la riga `Download`
   resta in stato `dispatched` per sempre — nessun follow-up su "scaricato / in conflitto / errore".
5. **Wedge del worker** (già mitigato in `feat/download-reliability`): un download appeso
   bloccava l'unico worker per ore. Introdotti fail-fast su "no provider" + `time_limit`.
6. **Full-sync mode "creduto"**: `mode: "full"` (storage e sync provider puntano alla stessa
   libreria) è un flag manuale non verificato → misconfigurazioni silenziose.

### Vincolo trasversale non negoziabile — Golden Rule

Nessun nome di provider può comparire in `core/`, `routes/`, `workers/`, `db/` o frontend
(vedi `CLAUDE.md`). Ogni capacità nuova va aggiunta come **metodo opzionale sul contratto** in
`core/interfaces.py`, con l'orchestrazione agnostica nei `workers/` che itera sul registry.
Le implementazioni provider-specifiche vivono in `providers/`. L'unica eccezione ammessa sono
gli **shim di route inbound** (come la route che serve l'utility di cattura di un provider), in
attesa di redesign.

## Decision

Adottiamo un modello **capability-oriented**: un vocabolario di stato **unico e generico** per
i download, fasi avanzate guidate o dal worker (path diretto) o riportate dal provider (path
delegato), e un insieme di capability opzionali + utility di `core/` che compongono domini
diversi senza accoppiare il core ai provider concreti.

### D0 — Tutto configurabile, niente magic number nel codice

Ogni soglia, timeout, tolleranza, toggle e feature opzionale introdotti da questo ADR (es. soglia
di confidenza AcoustID, tolleranza durata, livello di verifica post-upload, max-age della
riconciliazione, rate-limit, retry…) **non deve essere una costante hardcoded nella logica**. Si
dichiara in un **catalogo di impostazioni** centralizzato (i seed iniziali sono *dati editabili*,
non costanti sparse), con valori persistiti e **modificabili a runtime**. Ogni impostazione è:

- **globale, editabile dagli admin** (il tipo di account governa *chi* può scrivere; l'architettura
  resta pronta a diventare per-ruolo in futuro senza cambiare i call-site);
- **ribaltata sul frontend** (UI data-driven dal catalogo: label/descrizione/tipo/gruppo);
- letta ovunque via `ConfigManager` (nessuna lettura diretta né numeri magici nel codice),
  con **reload a runtime** così le modifiche hanno effetto senza riavviare i processi.

### D1 — Vocabolario di stato unificato del download (rinomina `done → downloaded`)

Un'unica lista di stati generici sostituisce il modello a due assi. `Download.status` (già
`VARCHAR` libero) usa:

| stato | significato | direct (es. un provider che scarica da una libreria in streaming) | delegated (es. Lidarr) |
|---|---|---|---|
| `pending` | in coda | ✓ | ✓ |
| `downloading` | recupero audio in corso | scarica dalla sorgente | il sistema esterno scarica |
| `importing` | atterraggio in libreria | `storage.store()` | import/rename del delegato |
| `conflict` | traccia già presente, da risolvere | dedup interno | conflitto di import |
| `retrying` | fallimento transitorio | ✓ | ✓ |
| `downloaded` | **terminale OK** (ex `done`) | ✓ | ✓ |
| `failed` | terminale KO | ✓ | ✓ |

- Gli stessi stati per tutti; cambia solo **chi li guida**.
- `dispatched` **si mantiene** (decisione): è l'ultimo stato che *musizerr* decide per un downloader
  esterno, prima che arrivino gli stati riportati dal delegato. Non sappiamo se/quando il delegato
  risponderà col suo primo stato, quindi `dispatched` è il punto di consegna esplicito. La sequenza
  delegata è: `pending → dispatched → (downloading → importing → …) → downloaded|failed`, dove gli
  stati fra parentesi arrivano da `poll_state`/webhook se e quando il delegato li riporta.

### D2 — Due sorgenti che scrivono gli stessi stati

- **Fasi gestite da musizerr** (direct): il worker imposta lo stato *inline* (es. `importing`
  quando chiama `storage.store()`; `conflict` quando il dedup trova un doppione).
- **Fasi gestite da un sistema esterno** (delegated): il worker **apprende** lo stato da una
  capability opzionale. Default:

```python
# core/interfaces.py — DownloadProvider (opzionale, default = nessuna fase esterna)
def poll_state(self, handle: "DelegationHandle") -> "DelegationStatus | None":
    return None   # provider senza fasi asincrone => "risolto", il worker procede da sé
```

"Chi non ha fasi intermedie, la risoluzione torna semplicemente `None`/risolto."

### D3 — Riconciliazione dei download delegati: **push-webhook primario + poll di fallback**

- **Reificare la delega**: al dispatch, il provider salva un `handle` opaco (per Lidarr:
  l'`id` album + timestamp) nei `delegated_details` sulla riga `Download`.
- **Primario — push**: usare il sistema **Connect → Webhook** di Lidarr. Lidarr fa `POST` degli
  eventi (`Grab`, `Import`, `ImportFailure`, `Upgrade`, `Rename`, `Health`) a un endpoint
  musizerr. Auto-registrabile via `POST /api/v1/notification`. L'handler traduce il payload
  nella stessa transizione di stato generica del poll.
- **Fallback — poll leggero**: task di riconciliazione con `countdown` (pattern già usato da
  `like_synced_track_task`) per gli eventi persi/musizerr offline, con **max-age**: un album
  delegato può non completarsi mai → dopo N ore → `failed`.
- **Route inbound**: shim provider-specifico (eccezione ammessa), idealmente dentro il package
  del provider che si auto-registra. Token condiviso per validare la sorgente; **payload
  trattato come dato non fidato** (solo campi strutturati).

### D4 — Conflict/dedup: operazione **generica di libreria**, non risolta dal delegato

Lidarr **non** risolve i conflitti per qualità: la risoluzione è un'operazione sui file/metadati
della libreria, lato musizerr. Due livelli, con requisiti diversi:

| Operazione | Serve accesso ai byte? | Meccanismo |
|---|---|---|
| **Confronto qualità (keep-better)** | ❌ No | metadati del delegato (`mediaInfo`: bitrate/codec/quality) via API → **host-independent** |
| **Integrità (ffmpeg)** | ✅ Sì | leggere il file via `StorageProvider.stream()` |
| **Fingerprint (AcoustID)** | ✅ Sì | idem |

- Policy pura in `core/` (`rank_quality`: lossless > lossy, poi bitrate). Decisione nel worker.
- **Requisito chiave**: la dedup file-level richiede che la libreria sia raggiungibile da uno
  `StorageProvider`. **Non conta dove gira Lidarr**, conta che i file siano raggiungibili (nel
  setup attuale: `StorageProvider` FTP → home server dove Lidarr importa). Se non raggiungibili
  → ci si ferma alla dedup-by-metadata via API.
- **Nuove capability opzionali** (default no-op):
  - `StorageProvider.list(prefix, session) -> list[FileRef]` (enumerare la cartella-album);
  - `StorageProvider.size(relative, session) -> int | None` (check cheap post-upload);
  - `DownloadProvider.list_delegated_files(handle) -> list[FileRef]` (path + quality via API);
  - `DownloadProvider.delete_file(ref)` (per la risoluzione API-level cross-host).
- `resolve_conflict()` **non** è una capability del download provider (Lidarr non sa farlo):
  l'esecuzione è del worker via `StorageProvider.delete` (file-level) o `delete_file` (API-level).

### D5 — Nuovo tipo di provider: `IdentificationProvider` (fingerprint → identità)

Non un "utility provider" generico, ma un contratto **specifico** per identificare l'audio:

```python
# core/interfaces.py
class IdentificationProvider(BaseProvider, ABC):
    def identify(self, audio_path: str) -> "Identity | None": ...
    # Identity: recording_id, title, artist, score  — NIENTE album (vedi caveat)
```

- Prima implementazione: **AcoustID** (`providers/identification/acoustid.py`), API key come
  credenziale/config. **Costruito "inerte"**: tutta l'infrastruttura è presente ma la capability è
  **disabilitata finché non viene inserita una API key** dalla UI (graceful no-op senza key), così
  non blocca nulla in assenza di chiave.
- **Soglia**: applicare solo match con `score >= 0.90`; sotto soglia → non toccare nulla.
- **Caveat album**: usare **solo titolo + artista** dal fingerprint. Mai album/anno (un'impronta
  corrisponde a decine di recording: studio/live/karaoke/compilation → rovina album giusti).
  L'album resta a Lidarr / metadata provider.
- **Note tecniche**: forzare il binario `fpcalc` (`FPCALC=/path`) per evitare il leak di file
  descriptor di `audioread`; alzare `ulimit -n`; rispettare il rate-limit AcoustID (~3 req/s).
- Consumato **generica­mente** via `registry.all_identification()` sia dal worker
  (anti-mislabel) sia dal tool di manutenzione (dedup/tag-repair).

### D6 — Utility di `core/` (non provider) per operazioni locali deterministiche

Analisi/operazioni locali sui file, senza backend esterno intercambiabile → **utility**, non
provider (farne provider sarebbe over-engineering):

- `core/audio_integrity.py` → `verify_audio(path, expected_duration_ms=None, tolerance_s=...)`
  (ffmpeg `-v error -i F -f null -` + ffprobe durata). **ffmpeg già presente nel container.**
- `core/audio_quality.py` → `rank_quality(...)` (lossless > lossy, poi bitrate).
- `core/tagging.py` → `write_tags(path, title=None, artist=None, ...)` (per-formato; per i tag
  ci si appoggia a `mutagen` o a ffmpeg per i Vorbis comment) + `make_probe_audio(tags)` per D8.

### D7 — Verifica integrità **pre e post-upload**, config-gated

- **Pre-upload** (staging locale): dopo `resolver.resolve()` e **prima** di `storage.store()`:
  se corrotto/troncato → `_cleanup_staging()` + `retriable=True` (riusa il flag già introdotto)
  → retry; mai caricare un file rotto.
- **Post-upload**: verificare il file *come salvato*, riletto via `storage.stream()`; se fallisce
  → `storage.delete()` + `retriable` + retry.
- **Config generica a livelli**: `verify_after_upload: "off" | "size" | "full"`
  - `size` (default): confronto dimensione staging vs remota (`StorageProvider.size()`), cheap;
  - `full`: stream-back + ffmpeg (robusto, **raddoppia il transfer** — su FTP/Tailscale è un
    costo reale, quindi opt-in).

### D8 — Verifica automatica del **full-sync mode** (canary probe)

Sostituire il flag `mode: "full"` creduto con un fatto verificato, usando **solo contratti
esistenti** (`StorageProvider.store/delete` + `SyncProvider.trigger_scan/is_available`):

```python
H = f"musizerr-probe-{uuid4().hex}"          # metadati unici hashati
canary = make_probe_audio(title=H, artist=H) # utility core (ffmpeg), .ogg minimo valido
rel = f"Musizerr Probe/{H}.ogg"              # sotto remote_base_path (NIENTE dot-folder)
try:
    storage.store(canary, rel, storage_session)
    sync.trigger_scan(sync_session)
    found = poll(lambda: sync.is_available(H, H, sync_session), timeout=..., interval=...)
    if found:
        set_sync_mode("full")                # persistito nel config del sync provider
    return found
finally:
    storage.delete(rel, storage_session)     # ELIMINA SEMPRE
    sync.trigger_scan(sync_session)          # il sync scarta il fantasma
```

- Innesco: azione admin esplicita **o** validazione automatica quando l'utente sceglie `full`
  (si valida prima di persistere). Task Celery async con feedback SSE.
- Concerni: timeout generoso (scan Navidrome può essere lento → falso negativo), cleanup sempre
  (`finally`), evitare dot-folder, pulizia di probe stantii, scope `system_store`.

### D9 — Auto-like come **stato desiderato riconciliato** (non un task a tempo)

**Problema.** La prima implementazione dell'auto-like schedulava, dopo `trigger_scan`, un
`like_synced_track_task` con `countdown` + `max_retries=3`. Difetti: (a) lo stato "questa traccia
va likeata" viveva solo come task ETA **in memoria del worker** → un riavvio lo perdeva (bug reale:
un worker con codice stale non lo eseguiva affatto); (b) correva contro l'indicizzazione asincrona
del sync provider e **si arrendeva** dopo 3×60s, perdendo il like per sempre durante i flood; (c)
il blocco viveva nel ramo **sincrono** di `download_task`, quindi per i download **delegati**
(Lidarr) non veniva **mai** eseguito.

**Decisione.** Modellare il like come **stato desiderato durevole** che converge, riusando lo stesso
pattern di D3 (riconciliazione delegati):

- Tabella `PendingLike (user_id, track_id, sync_provider)` — l'esistenza del record È l'intento, la
  cancellazione è il completamento. Unica per tripla (idempotente). Migrazione `0009`.
- `core/autolike.decide_like_action(...)` — macchina a stati **pura** e provider-agnostica:
  `like / skip-no-account / give-up (oltre max-age) / retry`. Testata in modo esaustivo.
- `workers.maintenance.reconcile_like` — converge un marcatore: risolve la traccia sul sync
  provider; se trovata → `like_track` + cancella; altrimenti si **ri-accoda con backoff** fino a un
  **max-age** (parametri nel `settings_catalog`, niente magic number), poi abbandona. Idempotente.
- **Aggancio alla transizione `downloaded`** in **entrambi** i percorsi: la sync-phase di
  `download_task` (sincrono) **e** il terminale di `_apply_delegation_status` (delegato) — così
  l'auto-like funziona anche per Lidarr, cosa impossibile con l'approccio precedente.
- `reconcile_pending_likes` — sweep di sicurezza e **backfill di massa** (il "like a tutte le saved
  già presenti nel sync" diventa: crea i marcatori e lascia convergere; nessun code-path speciale).
- Trigger: **evento** (`downloaded`) per accendere, **auto-backoff** per convergere, **cron/sweep**
  solo come rete di sicurezza — copia del modello di `reconcile_delegation`.

**Conseguenze.** Durevole (sopravvive ai riavvii, a differenza degli ETA task), idempotente,
immune al lag di indicizzazione (max-age lungo), provider-agnostico (usa `is_available`/`like_track`
già nell'interfaccia) e coerente con l'architettura di riconciliazione già accettata.
`like_synced_track_task` resta come shim deprecato per i messaggi eventualmente in coda.

**Emendamento (single-flight).** Ogni innesco (sweep orario, preferito aggiunto, atterraggio)
avviava una chain in più, e ognuna si ri-accodava ogni 60 s: 6–7 chain parallele per marcatore,
800+ esecuzioni al minuto. Ora una sola chain per marcatore: `PendingLike.next_attempt_at`
(migrazione `0022`) è preso atomicamente a inizio esecuzione (`claim_pending_like`) e chi non lo
prende si ferma senza interrogare il sync provider; il ritentativo usa un backoff esponenziale
con jitter (dall'intervallo configurato fino a un'ora, `core.like_reconciliation.retry_delay`);
lo sweep riprende solo i marcatori senza chain viva (mai schedulati o in ritardo oltre una
tolleranza). Stesso principio per D3: il lock Redis di `reconcile_delegation` porta il token
della chain viva, preso al dispatch (`start_reconciliation`), non al primo tick.

## Options Considered

### Riconciliazione delegato — Option A: Polling puro

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost | Traffico/latenza costanti; ritardo nel rilevare eventi |
| Scalability | Ok su pochi album; spreco su molti |
| Team familiarity | Alta (pattern già presente) |

**Pros:** self-contained, nessuna route inbound, nessuna config Lidarr.
**Cons:** latenza, polling continuo, "non mi fa impazzire" (feedback utente).

### Riconciliazione delegato — Option B: Push-webhook (Connect) + poll di fallback ✅

| Dimension | Assessment |
|-----------|------------|
| Complexity | Med (route inbound + parsing payload) |
| Cost | Quasi zero a regime; poll solo di sicurezza |
| Scalability | Ottima (event-driven) |
| Team familiarity | Media (webhook standard Servarr) |

**Pros:** latenza minima, first-party, auto-registrabile via API Lidarr, converge sullo stesso
handler generico del poll.
**Cons:** richiede uno shim di route inbound + reachability Lidarr→musizerr (via Tailscale) +
gestione sicurezza/payload non fidato.

### Identificazione — "utility provider" generico vs `IdentificationProvider` specifico ✅

Scartato il "utility provider" tuttofare: una categoria di provider è definita dal **contratto/
capacità**, non da un'etichetta vaga. Un contratto preciso (`identify`) è agnostico ed estendibile.

### Identificazione — beets ❌

Scartato: il matching di beets pesa troppo i tag/nome esistenti e **conferma i mislabel** (sul
file "What I've Done" proponeva "What I've Done", non "Crawling"). Serve AcoustID **puro** sull'audio.

### Dedup — "potenziare Lidarr" ❌ vs operazione di libreria via `StorageProvider` ✅

Mettere la logica di dedup dentro l'integrazione Lidarr violerebbe l'agnosticismo e sbaglierebbe
il posto (il dedup riguarda la libreria, non il downloader). Lidarr resta un download provider
che al più *espone* i suoi file (metadata/percorsi) via capability generiche.

## Trade-off Analysis

- **Stato unico vs due assi**: lo stato unico dà all'utente una lista coerente e allinea provider
  eterogenei, al costo di una **migrazione** (`done→downloaded`) che tocca codice, dati e UI. Vale
  la coerenza.
- **Push vs poll**: il push riduce latenza e traffico ma introduce una superficie inbound e una
  dipendenza dalla reachability. Il fallback poll neutralizza il rischio di eventi persi.
- **Dedup by-metadata vs by-bytes**: la by-metadata (API) è host-independent e copre il grosso del
  Problema 2 senza accesso ai file; la by-bytes (integrità/fingerprint) è più potente ma richiede
  raggiungibilità via `StorageProvider` e costa banda su Tailscale → confinata a manutenzione
  config-gated.
- **AcoustID**: alta affidabilità sull'identità (titolo/artista), ma dipendenza esterna, API key,
  rate-limit e nuove dipendenze di sistema (chromaprint/fpcalc). Opt-in.

## Consequences

**Più facile:**
- Nessun file corrotto/mislabeled entra in libreria; retry mirati (riusa `retriable`).
- Visibilità reale dello stato dei download delegati (UI data-driven).
- Full-sync mode verificato, non creduto.
- Dedup per qualità possibile anche con Lidarr su altro host.

**Più difficile / nuovi oneri:**
- Migrazione `done→downloaded` (codice + dati + frontend) da fare in modo completo.
- Nuova superficie inbound (webhook) con sicurezza e payload non fidato.
- Nuove dipendenze di sistema per AcoustID (immagine Docker) + gestione rate-limit/fd.
- Costo banda per le verifiche by-bytes su storage remoto.

**Da rivedere in futuro:**
- Se e come promuovere lo shim webhook dentro il package del provider (auto-registrazione).
- Canonicalizzazione dei nomi cartella su FS case-sensitive ("Mika" vs "MIKA") senza divergere
  dallo schema di rename di Lidarr.

## Action Items

**Fase 1 — su `feat/download-reliability` (nessuna nuova dipendenza):**
1. [ ] `core/audio_integrity.py` + verifica **pre-upload** in `download_task` (retry via `retriable`).
2. [ ] Verifica **post-upload** + `StorageProvider.size()` opzionale + config `verify_after_upload`.
3. [ ] Vocabolario di stato unificato + rinomina `done→downloaded` (codice, **migrazione Alembic
       dati**, frontend). Stati `downloading/importing` per il path diretto.
4. [ ] Remediation una-tantum: ri-accodare gli 83 file corrotti (verificati dal nuovo check).

**Fase 2 — riconciliazione delegato:**
5. [ ] Reificare l'handle di delega su `Download` + stato `conflict`.
6. [ ] Capability `poll_state` / `list_delegated_files` / `delete_file` (default no-op) + impl Lidarr.
7. [ ] Shim webhook inbound (token, payload non fidato) + auto-registrazione via API Lidarr.
8. [ ] Task di riconciliazione: webhook primario + poll di fallback + max-age.
9. [ ] Policy `rank_quality` + risoluzione conflitti (file-level via StorageProvider / API-level).

**Fase 3 — identificazione & manutenzione (branch/PR separato):**
10. [ ] `IdentificationProvider` + AcoustID (deps Dockerfile, API key, rate-limit, `FPCALC`).
11. [ ] Anti-mislabel post-download (soglia ≥0.90, solo titolo/artista) nel worker.
12. [ ] Tool di manutenzione: dedup qualità + tag-repair (`core/tagging.py`), `StorageProvider.list()`.

**Trasversale:**
13. [ ] Verifica full-sync (canary probe) come azione admin + validazione all'attivazione di `full`.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
