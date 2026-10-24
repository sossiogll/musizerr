"""
Task di manutenzione, provider-agnostici, che compongono più domini di provider.

- verifica automatica del *full-sync mode* (il presupposto che lo storage
  provider punti alla stessa libreria del sync provider) tramite un canary
  probe — usa solo i contratti StorageProvider + SyncProvider;
- riconciliazione dei download delegati (reconcile_delegation) e la sua
  rete di sicurezza periodica (sweep_delegations) — usa solo DownloadProvider;
- riconciliazione dei like come stato desiderato: preferiti all'arrivo di un
  download e propagazione del like al sync provider (reconcile_like /
  reconcile_pending_likes / propagate_unlike) — usa solo SyncProvider.
"""

from __future__ import annotations

import logging
import random
import time
from uuid import uuid4

import redis as redis_lib
from sqlmodel import Session

from datetime import datetime, timedelta, timezone

from db.engine import engine
from db.download_store import find_open_download, latest_landing
from db.models import Download, TrackRecord
from db.settings_store import SettingsRepository
from db.track_store import load_track
from musizeer_sdk.models import Track
from core.settings_resolver import namespace_provider_key
from core.backoff import retry_delay
from core.tagging import make_probe_audio
from core import download_states as ds, environment
from services.auth import auth_manager_for
from services.credentials import has_own_account, own_store, shared_store
from services.delegation import poll_for_requested_track
from services.enrichment import check_sync_availability
from services.progress import publish_download_progress
from services.stream import resolve_sync_id
from services import held_uploads, library_reconcile, playlist_downloads, update_check
from services.workspace import open_workspace, remove_stale_workspaces

from .celery_app import celery, get_config, get_registry

logger = logging.getLogger(__name__)

# Stesso Redis del broker/backend Celery (vedi workers/celery_app.py) — usato
# qui SOLO come lock leggero e a scadenza per far coesistere lo sweep di
# sicurezza (sweep_delegations) con le chain di riconciliazione già vive
# senza duplicarle. Vedi start_reconciliation/sweep_delegations più sotto.
_REDIS_URL = environment.redis_url()
_redis_client = redis_lib.Redis.from_url(_REDIS_URL)


@celery.task(name="workers.maintenance.verify_full_sync")
def verify_full_sync(sync_name: str | None = None) -> dict:
    """
    Canary probe: scrive un file di prova con metadati unici via storage,
    triggera lo scan del sync provider e verifica che compaia. Se sì attiva
    stabilmente ``mode: full`` sul sync provider. Elimina SEMPRE il file di prova.
    """
    config = get_config()
    registry = get_registry()

    sync_name = sync_name or config.sync_provider
    if not sync_name:
        return {"status": "error", "detail": "nessun sync provider configurato"}

    sync = registry.get_sync_provider(sync_name)
    storage = registry.get_storage_provider(config.storage_provider)
    if sync is None:
        return {"status": "error", "detail": f"sync provider '{sync_name}' non trovato"}
    if storage is None:
        return {"status": "error", "detail": "storage provider non trovato"}

    with Session(engine) as session:
        store = shared_store(session)
        auth = auth_manager_for(store)
        try:
            sync_session = auth.authenticate(sync)
            storage_session = auth.authenticate(storage)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"autenticazione fallita: {exc}"}

        token = uuid4().hex
        marker = f"musizeer-probe-{token}"
        # Track "canary" per il match universale: titolo/artista/album unici +
        # durata danno a match_track le conferme che richiede oltre al titolo.
        canary_track = Track(title=marker, artist=marker, album=marker, duration_ms=1000)
        # Sotto remote_base_path (dove vanno i download veri); niente dot-folder
        # (i sync provider possono ignorare le cartelle nascoste).
        rel = f"Musizeer Probe/{marker}.ogg"

        with open_workspace(config.workspace_dir, storage.name) as workspace:
            if not make_probe_audio(marker, marker, album=marker, path=str(workspace.path / "probe.ogg")):
                return {"status": "error", "detail": "generazione file di prova fallita"}
            found = False
            try:
                storage.store(workspace.file("probe.ogg"), rel, storage_session)
                sync.trigger_scan(sync_session)

                deadline = time.time() + config.full_sync_probe_timeout_seconds
                interval = config.full_sync_probe_interval_seconds
                while time.time() < deadline:
                    try:
                        if check_sync_availability(sync, canary_track, sync_session):
                            found = True
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("probe availability error: %s", exc)
                    time.sleep(interval)

                if found:
                    SettingsRepository(session).set_system(namespace_provider_key(sync_name, "mode"), "full")
                    logger.info("Full-sync verificato per '%s' → mode=full attivato", sync_name)
                else:
                    logger.info("Full-sync NON verificato per '%s' (file di prova non comparso)", sync_name)

                return {"status": "done", "full_sync": found, "sync_provider": sync_name}
            finally:
                # Cleanup SEMPRE: file di prova rimosso dallo storage e sync
                # ripulito (la cartella di lavoro la rimuove open_workspace).
                try:
                    storage.delete(rel, storage_session)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cleanup probe: delete storage fallito: %s", exc)
                try:
                    sync.trigger_scan(sync_session)
                except Exception:  # noqa: BLE001
                    pass


@celery.task(name="workers.maintenance.reconcile_delegation")
def reconcile_delegation(download_id: str, chain: str | None = None) -> dict:
    """
    UNA chain per download (single-flight): il lock Redis del download tiene
    il token della chain viva (``start_reconciliation``). Un tick con
    ``chain`` è di quella chain e continua solo se il lock è ancora suo; un
    tick senza (un push via webhook, una chain accodata prima di questo
    schema) fa il suo poll ma si ri-accoda solo se nessuna chain è viva.
    Segue il ciclo di vita di un download delegato interrogando il provider
    (poll_delegation) e riflettendo lo stato sul record Download. Su conflitto
    applica la policy di qualità (tieni la copia migliore, mai su una
    scommessa — vedi _resolve_conflicts). Ri-accoda se stessa finché non
    raggiunge un terminale o il max-age. Push (webhook) e poll convergono su
    questa stessa logica via _apply_delegation_status.

    Robustezza (self-healing): un fallimento TRANSITORIO — autenticazione
    che solleva, o poll_delegation che solleva per segnalare "il sistema
    esterno non ha risposto" (vedi il contratto in musizeer_sdk.interfaces) — non
    deve mai interrompere la chain né finalizzare il record come failed:
    un'irraggiungibilità del delegato non dice nulla sull'esito reale del
    download. Si ri-accoda sempre in questi casi, SENZA consumare il
    max-age.

    Il max-age (``delegation_max_age_hours``) misura il tempo OSSERVATO, non
    quello trascorso dal dispaccio: solo i poll a cui il delegato ha
    risposto con uno stato non terminale contano, e ognuno copre al più due
    intervalli di poll dall'ultima risposta (``_record_answer``). Un'attesa
    senza poll — una coda piena, un worker fermo, un delegato muto — non lo
    consuma. Oltre il max-age osservato la delega fallisce (il delegato ha
    risposto a lungo senza esito: non ritentabile). Una delega che arriva
    all'attesa massima (``delegation_max_wait_hours`` dal dispaccio) senza
    abbastanza risposte si chiude fallita ma RITENTABILE: è scaduta senza
    una risposta vera.
    """
    config = get_config()
    registry = get_registry()

    with Session(engine) as session:
        record = session.get(Download, download_id)
        if not record:
            return {"status": "gone"}
        if record.status in ds.TERMINAL:
            return {"status": record.status}
        handle = record.delegation_handle
        provider_name = record.provider_used
        started = record.started_at or record.completed_at
        # Traccia di dominio per la richiesta di QUESTO record: un delegato
        # dispatchato a livello di pacchetto (es. album) risponde sul pacchetto,
        # e se la TRACCIA effettivamente richiesta è già scaricata lo decide
        # l'host (services.delegation). None se il record non è (più)
        # risolvibile a una Track — vale allora lo stato dell'intero pacchetto.
        track_record = session.get(TrackRecord, record.track_id)
        track = load_track(session, track_record) if track_record else None

    if not handle or not provider_name:
        return {"status": "no-handle"}

    # Il tick di una chain rinnova il lock PRIMA di qualunque chiamata di
    # rete (niente finestra in cui il record sembra orfano allo sweep mentre
    # un tentativo è in corso) — ma solo se la chain è ancora quella viva.
    if chain is not None and not _hold_chain(download_id, chain, config):
        return {"status": "superseded"}

    provider = registry.get_download_provider(provider_name)
    if provider is None:
        return {"status": "provider-gone"}

    with Session(engine) as session:
        auth = auth_manager_for(shared_store(session))
        try:
            prov_session = auth.authenticate(provider)
        except Exception as exc:  # noqa: BLE001 — transitorio: credenziali non verificabili adesso
            logger.warning("reconcile auth failed for %s (transient, re-queuing): %s", provider_name, exc)
            if _expire_unanswered(download_id, started, config):
                return {"status": ds.FAILED, "detail": "max-wait"}
            return {"status": "auth-failed", "detail": _continue_chain(download_id, config, chain)}

    try:
        status = poll_for_requested_track(provider, handle, prov_session, track)
    except Exception as exc:  # noqa: BLE001 — il provider non ha potuto rispondere: transitorio, non un fallimento
        logger.warning("poll_delegation failed for %s (transient, re-queuing): %s", provider_name, exc)
        if _expire_unanswered(download_id, started, config):
            return {"status": ds.FAILED, "detail": "max-wait"}
        return {"status": "poll-failed", "detail": _continue_chain(download_id, config, chain)}

    if status is None:
        # Provider fire-and-forget: nessun ciclo asincrono da seguire → risolto.
        return {"status": "unsupported"}

    terminal = _apply_delegation_status(download_id, provider, prov_session, status, config, registry)
    if terminal:
        return {"status": status.state}

    # Non terminale E il provider ha risposto: solo questo tempo consuma il
    # max-age (il delegato ha parlato, quindi il tempo senza esito conta).
    observed = _record_answer(download_id, started, config)
    if observed > config.delegation_max_age_hours * 3600:
        _finalize(download_id, ds.FAILED, MAX_AGE_DETAIL)
        return {"status": ds.FAILED, "detail": "max-age"}
    if _expire_unanswered(download_id, started, config):
        return {"status": ds.FAILED, "detail": "max-wait"}

    return {"status": status.state, "detail": _continue_chain(download_id, config, chain)}


MAX_AGE_DETAIL = "delegation timed out (no result after the max age of answered polls)"
MAX_WAIT_DETAIL = "delegation expired without an answer (max wait exceeded)"


def _record_answer(download_id: str, started, config) -> float:
    """Il delegato ha risposto (non terminale): conta la risposta e il tempo
    che copre — dall'ultima risposta (o dal dispaccio), al più due intervalli
    di poll. Ritorna il tempo osservato in tutto."""
    cap = 2 * config.delegation_poll_interval_seconds
    now = _now()
    with Session(engine) as session:
        record = session.get(Download, download_id)
        if record is None:
            return 0.0
        since = record.delegation_answered_at or started
        gap = _age_seconds(since) if since is not None else None
        record.delegation_observed_seconds = (record.delegation_observed_seconds or 0.0) + min(max(gap or 0.0, 0.0), cap)
        record.delegation_answers = (record.delegation_answers or 0) + 1
        record.delegation_answered_at = now
        session.add(record)
        session.commit()
        return record.delegation_observed_seconds


def _expire_unanswered(download_id: str, started, config) -> bool:
    """Oltre l'attesa massima dal dispaccio la delega si chiude, fallita ma
    ritentabile: non è arrivata a un esito né a abbastanza risposte per
    dirlo. True se l'ha chiusa."""
    age = _age_seconds(started)
    if age is None or age <= config.delegation_max_wait_hours * 3600:
        return False
    _finalize(download_id, ds.FAILED, MAX_WAIT_DETAIL, retriable=True)
    return True


def _reconcile_lock_key(download_id: str) -> str:
    return f"musizeer:delegation-reconcile-lock:{download_id}"


def _lock_ttl(config, countdown: int) -> int:
    """Il lock copre l'attesa del prossimo tick più un margine ampio (2x il
    poll) per jitter e rallentamenti; scade da solo se la chain muore."""
    return int(countdown) + max(config.delegation_poll_interval_seconds * 2, 60)


def start_reconciliation(download_id: str, config, countdown: int | None = None) -> bool:
    """
    Avvia la chain di riconciliazione di un download delegato, se non ce
    n'è già una viva. Il lock (col token della nuova chain) si prende ADESSO,
    non al primo tick: il primo tick arriva dopo ``countdown`` (di norma
    l'intervallo di poll), e uno sweep nel frattempo non deve scambiare la
    chain in attesa per un orfano. False se una chain era già viva.

    Se Redis non risponde si avvia comunque: meglio un doppione occasionale
    (si spegne al primo tick che trova il lock di un'altra) che un orfano.
    """
    countdown = config.delegation_poll_interval_seconds if countdown is None else countdown
    token = uuid4().hex
    try:
        taken = bool(_redis_client.set(
            _reconcile_lock_key(download_id), token, nx=True, ex=_lock_ttl(config, countdown),
        ))
    except Exception as exc:  # noqa: BLE001 — Redis irraggiungibile non deve bloccare la riconciliazione
        logger.warning("reconcile lock for %s not taken, starting anyway: %s", download_id, exc)
        taken = True
    if taken:
        reconcile_delegation.apply_async(kwargs={"download_id": download_id, "chain": token}, countdown=countdown)
    return taken


def _hold_chain(download_id: str, chain: str, config) -> bool:
    """La chain ``chain`` è ancora quella viva? Se sì ne rinnova il lock. Un
    lock scaduto (worker fermo più del TTL) torna a chi lo aveva."""
    key = _reconcile_lock_key(download_id)
    try:
        owner = _redis_client.get(key)
        if owner is not None and owner.decode() != chain:
            logger.info("reconcile chain %s of %s superseded, stopping", chain, download_id)
            return False
        _redis_client.set(key, chain, ex=_lock_ttl(config, 0))
    except Exception as exc:  # noqa: BLE001 — vedi start_reconciliation
        logger.debug("reconcile lock check failed for %s: %s", download_id, exc)
    return True


def _continue_chain(download_id: str, config, chain: str | None) -> str:
    """Il prossimo tick: della stessa chain, oppure — per un tick esterno —
    una chain nuova solo se nessuna è viva."""
    if chain is None:
        return "re-queued" if start_reconciliation(download_id, config) else "chain-alive"
    countdown = config.delegation_poll_interval_seconds
    try:
        _redis_client.set(_reconcile_lock_key(download_id), chain, ex=_lock_ttl(config, countdown))
    except Exception as exc:  # noqa: BLE001 — vedi start_reconciliation
        logger.debug("reconcile lock touch failed for %s: %s", download_id, exc)
    reconcile_delegation.apply_async(kwargs={"download_id": download_id, "chain": chain}, countdown=countdown)
    return "re-queued"


@celery.task(name="workers.maintenance.sweep_delegations")
def sweep_delegations() -> dict:
    """
    Rete di sicurezza periodica (beat) per i download delegati: trova ogni
    Download non terminale con un delegation_handle e, se nessuna chain di
    riconciliazione risulta viva per lui, ne avvia una. Copre qualunque
    motivo per cui la self-rescheduling chain di reconcile_delegation si sia
    interrotta — crash/riavvio del worker che perde il countdown task in
    coda su Redis, un'eccezione non prevista che scappa ai try/except
    espliciti sopra, il caso reale che ha motivato questo modulo (8 giorni
    di record fermi in 'dispatched' con un max-age di 12h).

    COESISTENZA con una chain già viva (scelta deliberata, documentata qui
    perché non ovvia): il lock Redis del download tiene il token della chain
    viva, preso quando la chain parte (``start_reconciliation``, anche al
    dispatch, prima che il primo tick sia dovuto) e rinnovato a ogni tick
    (TTL = attesa del prossimo tick + margine). Nessuna introspezione del
    broker: un task Celery con ETA futura non è altrimenti visibile prima di
    partire, quindi "il lock è fresco" è il proxy più economico disponibile
    per "qualcuno ci sta già lavorando". Solo un lock scaduto — nessun tick
    recente, la chain è morta per davvero — fa partire una chain nuova,
    subito (la ripresa deve essere pronta, non aspettare un altro giro).

    Idempotente e cheap: una query indicizzata (delegation_handle IS NOT
    NULL AND status NOT IN terminal) + un SET NX Redis per candidato; sweep
    ripetuti su un record già ripreso trovano il lock e non fanno nulla. Se
    Redis stesso non risponde, si riprende comunque (rischio: una chain
    duplicata occasionale durante un'interruzione di Redis) piuttosto che
    lasciare un vero orfano bloccato per sempre — il doppione si spegne al
    primo tick che trova il lock di un'altra chain.
    """
    from sqlmodel import select

    config = get_config()
    with Session(engine) as session:
        candidate_ids = session.exec(
            select(Download.id).where(
                Download.delegation_handle.is_not(None),  # type: ignore[attr-defined]
                Download.status.not_in(tuple(ds.TERMINAL)),  # type: ignore[attr-defined]
            )
        ).all()

    resumed = 0
    for download_id in candidate_ids:
        if start_reconciliation(download_id, config, countdown=0):
            resumed += 1

    if resumed:
        logger.info("sweep_delegations: %d/%d download delegati orfani ripresi", resumed, len(candidate_ids))
    return {"status": "swept", "checked": len(candidate_ids), "resumed": resumed}


def _apply_delegation_status(download_id, provider, prov_session, status, config, registry) -> bool:
    """Riflette lo stato del delegato sul record. Ritorna True se terminale."""
    state = status.state

    # Risoluzione conflitti (policy qualità, metadata-level host-independent).
    # Gated dal toggle "dedup_enabled" (default: off) — senza questo controllo
    # musizeer cancellava file via l'API del delegato anche quando l'utente
    # aveva la dedup esplicitamente disattivata. Se disabilitata il conflitto
    # resta ds.CONFLICT: nessuna eliminazione automatica, il download andrà
    # in timeout (max-age) se non risolto altrove.
    if state == ds.CONFLICT and status.conflicts and config.dedup_enabled:
        all_resolved = _resolve_conflicts(provider, prov_session, status.conflicts, config)
        if all_resolved:
            # Tutti i conflitti hanno avuto una decisione netta: si procede.
            state = ds.IMPORTING
        # Altrimenti almeno un conflitto è rimasto UNDECIDED (bitrate/sample
        # rate non riportato da un lato): resta ds.CONFLICT — nessun
        # avanzamento automatico su una scommessa. Serve intervento umano
        # (o un delegato che riporti metadati migliori a un poll successivo).

    with Session(engine) as session:
        record = session.get(Download, download_id)
        if not record or record.status in ds.TERMINAL:
            return True
        if record.status == state:
            # Nulla è cambiato: niente scrittura, niente evento di progresso.
            return False
        record.status = state
        if state in ds.TERMINAL:
            record.completed_at = datetime.now(timezone.utc)
            if state == ds.FAILED:
                record.retriable = False
                if status.detail:
                    record.error_message = status.detail
        session.add(record)
        session.commit()
        track_id = record.track_id

        # È QUI che un download delegato "atterra" davvero in libreria: chi
        # l'ha chiesto con il preferito lo trova nei preferiti, e il like
        # parte verso il sync provider.
        if state == ds.DOWNLOADED:
            from services.favorites import apply_intents

            apply_intents(session, track_id)

    publish_download_progress(download_id, state, "", track_id, registry)
    return state in ds.TERMINAL


def _resolve_conflicts(provider, prov_session, conflicts, config) -> bool:
    """
    Per ogni conflitto usa la policy pura ``core.audio_quality.decide_dedup``
    per decidere quale copia tenere, ed elimina l'altra via API del delegato
    (senza leggere i byte). Se la policy non riesce a decidere con certezza
    (bitrate o sample rate non riportato da un lato, a parità di classe
    lossless) il conflitto resta IRRISOLTO — non si cancella un file reale
    sulla base di un'informazione mancante.

    Ritorna True solo se OGNI conflitto ha avuto una decisione netta (non
    necessariamente che ``delete_file`` sia riuscita: quel fallimento è un
    problema separato, già loggato, e non retrocede la decisione di qualità
    a "non decisa").

    Vedi la nota in ``core/audio_quality.py``: eseguito solo quando il
    provider popola ``DelegationStatus.conflicts`` con FileRef concreti —
    nessun provider album-level di prima parte lo fa oggi.
    """
    from core.audio_quality import decide_dedup, KEEP_INCOMING, UNDECIDED

    prefer_lossless = config.dedup_prefer_lossless
    all_decided = True
    for c in conflicts:
        if not c.incoming or not c.existing:
            continue
        decision = decide_dedup(
            incoming_lossless=c.incoming.quality_lossless,
            incoming_bitrate=c.incoming.bitrate,
            incoming_sample_rate=c.incoming.sample_rate,
            existing_lossless=c.existing.quality_lossless,
            existing_bitrate=c.existing.bitrate,
            existing_sample_rate=c.existing.sample_rate,
            prefer_lossless=prefer_lossless,
        )
        if decision.action == UNDECIDED:
            all_decided = False
            logger.warning(
                "Dedup '%s': conflitto NON risolto (%s) — tengo entrambe le copie: %s / %s",
                c.track_key, decision.reason, c.incoming.path, c.existing.path,
            )
            continue

        loser = c.existing if decision.action == KEEP_INCOMING else c.incoming
        winner = c.incoming if decision.action == KEEP_INCOMING else c.existing
        try:
            provider.delete_file(loser, prov_session)
            logger.info(
                "Dedup '%s' (%s): tengo %s, elimino %s",
                c.track_key, decision.reason, winner.path, loser.path,
            )
        except NotImplementedError:
            logger.debug("Provider non supporta delete_file: conflitto '%s' non risolto via API", c.track_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_file fallito per '%s': %s", c.track_key, exc)
    return all_decided


def _finalize(download_id: str, state: str, detail: str | None = None, *, retriable: bool = False) -> None:
    with Session(engine) as session:
        record = session.get(Download, download_id)
        if not record or record.status in ds.TERMINAL:
            return
        record.status = state
        record.completed_at = datetime.now(timezone.utc)
        if state == ds.FAILED:
            # Il delegato ha risposto per tutto il max-age senza concludere:
            # riprovarlo identico non cambierebbe nulla. Scaduta senza una
            # risposta vera (``retriable``), un nuovo tentativo può riuscire.
            record.retriable = retriable
        if detail:
            record.error_message = detail
        session.add(record)
        session.commit()


def _age_seconds(ts) -> float | None:
    if ts is None:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now() - ts).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _like_age_seconds(registered_at, landed_at) -> float | None:
    """Il max-age di un like misura l'attesa dell'indicizzazione, quindi
    parte dall'evento più recente: la registrazione dell'intento o l'ultimo
    atterraggio della traccia (chi si aggancia registra prima di atterrare)."""
    ages = [a for a in (_age_seconds(registered_at), _age_seconds(landed_at)) if a is not None]
    return min(ages) if ages else None


# ------------------------------------------------------------------
# Riconciliazione dei like: stato desiderato
# ------------------------------------------------------------------
#
# Sostituisce il vecchio scheduling "racy" del like (task ETA in memoria, che
# spariva a un riavvio del worker e si arrendeva dopo 3 tentativi). Qui il like
# è uno STATO DESIDERATO durevole (PendingLike):
#   - acceso alla transizione di stato "downloaded" (sia sincrono sia delegato),
#   - convergente con backoff finché la traccia non è indicizzata nel sync,
#   - abbandonato solo oltre un max-age configurabile,
#   - idempotente (ri-applicarlo non fa doppi like; a fine converge e si spegne).
# Stesso pattern di reconcile_delegation, esteso al like.
#
# Single-flight: UNA chain per marcatore. Ogni esecuzione prende prima il
# marcatore (``claim_pending_like``: ``next_attempt_at`` NULL o scaduto);
# chi non lo prende — un secondo innesco da favorites, lo sweep, una chain
# duplicata — si ferma subito, senza cercare nulla sul sync provider. Chi lo
# prende lo tiene per _LIKE_RUN_HOLD_SECONDS e, se deve ritentare, lo
# rischedula lui stesso con backoff (core.like_reconciliation.retry_delay).

# Quanto un'esecuzione tiene il marcatore: ben oltre una ricerca sul sync
# provider; se il worker muore a metà, lo sweep lo riprende dopo.
_LIKE_RUN_HOLD_SECONDS = 600
# Un tentativo schedulato che non è partito entro questo ritardo è perso
# (worker riavviato): lo sweep avvia una chain nuova.
_LIKE_LOST_CHAIN_GRACE_SECONDS = 300
# Tolleranza fra l'ETA di Celery e l'orario scritto sul marcatore.
_LIKE_DUE_TOLERANCE_SECONDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


@celery.task(name="workers.maintenance.reconcile_like")
def reconcile_like(pending_like_id: str) -> dict:
    """Converge un singolo marcatore di like: un intento "preferito
    all'arrivo" si applica appena la traccia è in libreria; un intento di
    propagazione converge verso lo stato reale del sync provider."""
    from db.models import PendingLike
    from db.pending_like_store import claim_pending_like
    from core import like_reconciliation as rules
    from services.favorites import apply_intents, is_in_library

    config = get_config()
    registry = get_registry()
    now = _now()

    with Session(engine) as session:
        claimed = claim_pending_like(
            session, pending_like_id,
            due_by=now + timedelta(seconds=_LIKE_DUE_TOLERANCE_SECONDS),
            hold_until=now + timedelta(seconds=_LIKE_RUN_HOLD_SECONDS),
        )
        marker = session.get(PendingLike, pending_like_id)
        if not marker:
            return {"status": "gone"}
        if not claimed:
            return {"status": "not-due"}

        def release() -> None:
            # Nessuna chain da tenere viva: lo riprende lo sweep (o
            # l'atterraggio della traccia).
            marker.next_attempt_at = None
            session.add(marker)
            session.commit()

        if marker.sync_provider is None:
            # Download con preferito: si applica quando la traccia arriva (e
            # resta, per il nuovo tentativo, se il download è fallito).
            if is_in_library(session, marker.track_id, registry):
                release()
                apply_intents(session, marker.track_id)
                return {"status": "favorited"}
            release()
            return {"status": "awaiting-download"}

        sync = registry.get_sync_provider(marker.sync_provider)
        if not sync:
            session.delete(marker)
            session.commit()
            return {"status": "provider-gone"}

        # Intento registrato da chi si è agganciato a una richiesta ancora in
        # corso: niente da cercare finché la traccia non atterra, e sarà
        # l'atterraggio a riavviare questo reconciler.
        if find_open_download(session, marker.track_id):
            release()
            return {"status": "awaiting-download"}

        # The like is personal data: the user's own account of the sync
        # provider, never a shared one (``own_store``).
        store = own_store(session, marker.user_id)
        auth_manager = auth_manager_for(store)
        has_own = has_own_account(store, sync)

        # Risolvi solo se ha senso provarci (evita ricerche inutili sul sync).
        sync_id = resolve_sync_id(session, marker.track_id, sync, auth_manager, store) if has_own else None
        max_age = config.get("like_reconcile_max_age_hours", 6) * 3600
        action = rules.decide_like_action(
            has_own_account=has_own,
            sync_id=sync_id,
            age_seconds=_like_age_seconds(marker.created_at, latest_landing(session, marker.track_id)),
            max_age_seconds=max_age,
        )

        if action == rules.SKIP_NO_ACCOUNT:
            session.delete(marker)
            session.commit()
            logger.info(
                "Like skip: utente %s senza account personale su '%s'",
                marker.user_id, marker.sync_provider,
            )
            return {"status": "no-personal-account"}

        if action == rules.LIKE:
            try:
                sync_session = auth_manager.authenticate(sync)
                sync.like_track(sync_id, sync_session)
                session.delete(marker)
                session.commit()
                logger.info(
                    "Like applicato: track %s su '%s' per utente %s",
                    marker.track_id, marker.sync_provider, marker.user_id,
                )
                return {"status": "liked"}
            except Exception as exc:  # noqa: BLE001 — errore di rete: ritenta, non perdere il marcatore
                logger.warning("Like fallito su '%s' (riprovo): %s", marker.sync_provider, exc)
                action = rules.RETRY

        if action == rules.GIVE_UP:
            session.delete(marker)
            session.commit()
            logger.info(
                "Like abbandonato (max-age): track %s mai comparsa in '%s'",
                marker.track_id, marker.sync_provider,
            )
            return {"status": "gave-up"}

        # RETRY: il prossimo tentativo di QUESTA chain, con backoff
        # esponenziale e jitter; il marcatore resta preso fino ad allora.
        marker.attempts += 1
        delay = retry_delay(
            marker.attempts, config.get("like_reconcile_interval_seconds", 60), random.random(),
            rules.MAX_RETRY_INTERVAL_SECONDS,
        )
        marker.next_attempt_at = now + timedelta(seconds=delay)
        session.add(marker)
        session.commit()
        reconcile_like.apply_async(kwargs={"pending_like_id": pending_like_id}, countdown=delay)
        return {"status": "re-queued", "attempts": marker.attempts, "countdown": delay}


@celery.task(name="workers.maintenance.propagate_unlike")
def propagate_unlike(user_id: str, track_id: str, sync_provider: str) -> dict:
    """Il cuore tolto: toglie il like anche sul sync provider, se la traccia
    vi è indicizzata e l'utente ha un proprio account. Best effort: il file
    resta sempre in libreria."""
    registry = get_registry()
    sync = registry.get_sync_provider(sync_provider)
    if sync is None:
        return {"status": "provider-gone"}
    with Session(engine) as session:
        store = own_store(session, user_id)
        if not has_own_account(store, sync):
            return {"status": "no-personal-account"}
        auth_manager = auth_manager_for(store)
        sync_id = resolve_sync_id(session, track_id, sync, auth_manager, store)
        if not sync_id:
            return {"status": "not-indexed"}
        try:
            sync.unlike_track(sync_id, auth_manager.authenticate(sync))
        except Exception as exc:  # noqa: BLE001 — best effort
            logger.warning("Unlike su '%s' fallito: %s", sync_provider, exc)
            return {"status": "failed"}
    return {"status": "unliked"}


# Outcome of taking one imported like back on its origin provider.
UNLIKED = "unliked"
NOT_SUPPORTED = "not_supported"
NO_PERSONAL_ACCOUNT = "no_personal_account"
MISSING_PERMISSION = "missing_permission"
FAILED = "failed"
_UNLIKE_OUTCOMES = (UNLIKED, FAILED, NOT_SUPPORTED, NO_PERSONAL_ACCOUNT, MISSING_PERMISSION)


def _unlike_on_origin(provider, provider_track_id: str | None, store, auth_manager) -> tuple[str, str | None]:
    """(outcome, reason) of taking the like ``provider_track_id`` back on
    ``provider`` with the user's OWN account only: ``store`` holds no system
    fallback and the authentication never falls back to an app-only token."""
    from musizeer_sdk.capabilities import CAPABILITY_UNLIKE_SAVED_TRACK
    from services.credentials import own_missing_capabilities

    if provider is None or not provider_track_id or not provider.supports_unlike_saved_track():
        return NOT_SUPPORTED, None
    if not has_own_account(store, provider):
        return NO_PERSONAL_ACCOUNT, None
    if CAPABILITY_UNLIKE_SAVED_TRACK in own_missing_capabilities(store, provider):
        return MISSING_PERMISSION, "the account was connected without the permission to remove likes: reconnect it"
    try:
        provider.unlike_saved_track(provider_track_id, auth_manager.authenticate(provider))
    except PermissionError as exc:
        return MISSING_PERMISSION, str(exc)
    except Exception as exc:  # noqa: BLE001 — best effort
        return FAILED, str(exc) or type(exc).__name__
    return UNLIKED, None


@celery.task(name="workers.maintenance.unlike_on_origin_providers")
def unlike_on_origin_providers(user_id: str, track_id: str) -> dict:
    """The favorite is off: the likes the user imported from other providers
    for this track are taken off there too, on the providers whose API allows
    it (``MetadataProvider.supports_unlike_saved_track``) and only with the
    user's OWN connected account — never a shared system account nor an
    app-only token. Best effort: what was taken off is forgotten here (the
    imported like goes), what was not stays, and the removal is remembered by
    the host either way (``RemovedFavorite``), so a like still on the origin
    never makes the favorite come back. Does nothing unless THIS user turned
    on their setting ``unlike_on_origin_providers``.

    The result counts each outcome and lists, per provider, its outcome and
    the reason of a failure (``missing_permission``: the account must be
    reconnected to grant it)."""
    from sqlalchemy import or_
    from sqlmodel import select

    from db.models import UserSavedTrack, UserSavedTrackProvider
    from services.settings import personal_value

    registry = get_registry()
    with Session(engine) as session:
        if not personal_value(session, user_id, "unlike_on_origin_providers"):
            return {"status": "disabled"}
        rows = session.exec(
            select(UserSavedTrack, UserSavedTrackProvider)
            .join(UserSavedTrackProvider, UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
            .where(UserSavedTrack.user_id == user_id,
                   or_(UserSavedTrack.track_id == track_id, UserSavedTrack.library_track_id == track_id))
        ).all()
        store = own_store(session, user_id)
        auth_manager = auth_manager_for(store)
        result: dict = {"status": "done", **{outcome: 0 for outcome in _UNLIKE_OUTCOMES}, "providers": []}
        for saved, link in rows:
            provider = registry.get_metadata_provider(link.provider)
            outcome, reason = _unlike_on_origin(provider, link.provider_track_id, store, auth_manager)
            result[outcome] += 1
            result["providers"].append({"provider": link.provider, "outcome": outcome, "reason": reason})
            if outcome in (UNLIKED, NOT_SUPPORTED):
                logger.info("Unlike on origin, user %s, provider '%s': %s", user_id, link.provider, outcome)
            else:
                logger.warning("Unlike on origin, user %s, provider '%s': %s (%s)",
                               user_id, link.provider, outcome, reason or "no personal account connected")
            if outcome != UNLIKED:
                continue
            session.delete(link)
            session.flush()
            if not session.exec(select(UserSavedTrackProvider).where(UserSavedTrackProvider.saved_track_id == saved.id)).first():
                session.delete(saved)
        session.commit()
    return result


@celery.task(name="workers.maintenance.reconcile_pending_likes")
def reconcile_pending_likes() -> dict:
    """
    Sweep di sicurezza + backfill di massa: rimette in moto i marcatori
    pendenti SENZA una chain viva (mai partiti, rilasciati in attesa di un
    download, o con un tentativo schedulato e mai arrivato). Un marcatore
    con un tentativo in corso, in programma o già in coda non si tocca: non
    si avvia mai una seconda chain. È anche il modo pulito per riconciliare like persi:
    basta creare i marcatori e lasciar convergere il reconciler.
    """
    from db.pending_like_store import list_resumable_pending_like_ids, mark_queued

    now = _now()
    lost_before = now - timedelta(seconds=_LIKE_LOST_CHAIN_GRACE_SECONDS)
    queued = 0
    with Session(engine) as session:
        for pid in list_resumable_pending_like_ids(session, lost_before):
            # Segnato in coda PRIMA di accodarlo: uno sweep successivo (un
            # riavvio, una run in ritardo) non accoda un secondo tentativo
            # mentre questo aspetta il worker.
            if mark_queued(session, pid, now, lost_before=lost_before):
                reconcile_like.apply_async(kwargs={"pending_like_id": pid})
                queued += 1
    return {"status": "swept", "count": queued}


# Una cartella di lavoro vive quanto un download o uno streaming; oltre il
# doppio del limite rigido di un download (e comunque almeno un'ora) è
# rimasta da un processo interrotto.
_WORKSPACE_MIN_AGE_SECONDS = 3600


@celery.task(name="workers.maintenance.sweep_workspaces")
def sweep_workspaces() -> dict:
    """Rimuove le cartelle di lavoro orfane (vedi services/workspace.py) e i
    file in attesa di upload da più di ``held_upload_max_age_hours``
    (services/held_uploads.py)."""
    config = get_config()
    max_age = max(2 * config.download_hard_time_limit_seconds, _WORKSPACE_MIN_AGE_SECONDS)
    removed = remove_stale_workspaces(config.workspace_dir, max_age)
    held = held_uploads.remove_stale(config.workspace_dir, config.held_upload_max_age_hours * 3600)
    return {"status": "ok", "removed": removed, "held_removed": held}


@celery.task(name="workers.maintenance.check_for_updates")
def check_for_updates() -> dict:
    """Reads the releases of Musizeer and keeps the newer ones with their metadata (services/update_check.py):
    Administration judges them against what runs. A feed that cannot be read (the repository is private or
    renamed, a rate limit, GitHub or the network unreachable) is an ``ok`` run with ``state: "unavailable"`` and
    its ``reason``; only an answer that is not a list of releases fails the run."""
    return update_check.check()


@celery.task(name="workers.maintenance.reconcile_library_files")
def reconcile_library_files() -> dict:
    """Riconcilia le voci della libreria con i file che lo storage contiene
    davvero (services/library_reconcile.py): un file spostato si ritrova,
    uno sparito segna la voce come mancante. Nessun file viene cancellato."""
    with Session(engine) as session:
        report = library_reconcile.reconcile_library_files(
            session, get_registry(), get_config(), auth_manager_for(shared_store(session)), apply=True,
        )
    return report.summary()


@celery.task(name="workers.maintenance.delete_playlist_tracks")
def delete_playlist_tracks(user_id: str, track_ids: list[str]) -> dict:
    """I file che una playlist aveva fatto scaricare, cancellati insieme a lei
    su richiesta del proprietario (``services.playlist_downloads``): solo quelli
    che nessuna playlist e nessun preferito tiene, e solo file scaricati da
    Musizeer per questo utente."""
    registry = get_registry()
    with Session(engine) as session:
        shared = auth_manager_for(shared_store(session))
        return playlist_downloads.delete_downloads(
            session, registry, get_config(), shared, user_id, track_ids,
        )
