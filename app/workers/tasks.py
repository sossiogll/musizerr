from __future__ import annotations
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from celery import Task
from sqlmodel import Session, select

from db.engine import engine
from db.library_store import in_library
from db.models import Download, LibraryEntry
from db.settings_store import SettingsRepository
from db.upsert import upsert
from core.auth_manager import AuthError
from core.download_resolver import DownloadResolver, MatchConfidence, ResolveResult
from core.audio_integrity import verify_audio
from core.backoff import retry_delay
from core import download_states as ds
from musizerr_sdk.models import Track
from core.routines import SYNC_LIBRARY_INDEX
from core.utc import as_utc
from services.auth import auth_manager_for
from services.storage import resolve_storage_provider
from core.settings_resolver import provider_config_from_system_settings

from .celery_app import celery, get_config, get_registry
from db import library_job_store as job_store
from services import library_jobs
from services.credentials import account_holders, has_own_account, own_store, shared_store
from services import downloads as download_service
from services import favorites, playlist_monitor, playlist_sync, routines, sync_index
from services.download_requests import liked_elsewhere_spec, request_download
from services.enrichment import cross_enrich_track
from services.errors import public_error
from services.identification import identify_confidently
from services.library_paths import library_path
from services.progress import publish_download_progress
from services.provider_cooldown import RedisProviderCooldown
from services.provider_slots import RedisProviderSlots
from services.track_state import sync_id_types
from services import held_uploads
from services.workspace import WorkspaceError, open_workspace, open_workspaces

logger = logging.getLogger(__name__)


class _DownloadTask(Task):
    """Base class con logica di retry e aggiornamento stato nel DB."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Un'eccezione non prevista (time limit, errore del worker) non dice
        # che la traccia non esiste: il routine dei transitori la riprova.
        _update_download_status(
            download_id=kwargs.get("download_id"),
            status=ds.FAILED,
            error_message=str(exc),
            retriable=True,
        )

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        _update_download_status(
            download_id=kwargs.get("download_id"),
            status=ds.RETRYING,
            error_message=str(exc),
            retry_count=self.request.retries,
        )


@celery.task(
    bind=True,
    base=_DownloadTask,
    name="workers.tasks.download_task",
    max_retries=10,   # safe default; la config a runtime può limitare prima
    # Un singolo download non deve mai bloccare l'unico worker: se un provider
    # si appende (es. sessione remota che entra in loop di reconnect), il soft
    # limit prova a interromperlo con un'eccezione catturabile, e il time_limit
    # duro è la rete di sicurezza che uccide il child del worker. I download
    # reali stanno sotto ~70s, quindi 4/5 min non toccano mai un caso legittimo.
    # NB: limiti per-task — lo scan library_download_sync (coda 'jobs') resta
    # senza limiti perché può durare a lungo.
    # I valori non sono qui: il decoratore si valuta una volta all'import. Li passa
    # queue_download() a ogni accodamento.
)
def download_task(
    self,
    track_data: dict,
    download_id: str,
    user_id: str | None = None,
    force: bool = False,
) -> dict:
    """
    Task generico di download. Riceve la Track canonica serializzata
    (``services.downloads.task_track_data``) e l'ID del record Download nel
    DB. Il resolver sceglie autonomamente il provider.

    Ritorna un dict con il risultato per il result backend Celery.
    """
    config = get_config()
    registry = get_registry()

    track = Track.from_dict(track_data)

    def progress(status: str, **extra) -> None:
        publish_download_progress(download_id, status, track.title, track.internal_id, registry, **extra)

    retry_num = self.request.retries

    # La traccia è arrivata in libreria da un'altra strada mentre questa
    # richiesta aspettava (una scansione, il sync provider, un altro
    # download): non si scarica una seconda copia. Solo un admin forza.
    if not force and _in_library(track.internal_id, registry):
        held_uploads.release(config.workspace_dir, track.internal_id)
        _update_download_status(download_id, status=ds.DOWNLOADED, error_message="already in the library")
        _on_landed(download_id)
        progress(ds.DOWNLOADED)
        logger.info("'%s' is already in the library: nothing to download (download_id=%s)", track.title, download_id)
        return {"status": ds.DOWNLOADED, "detail": "already_in_library"}

    _update_download_status(download_id, status=ds.DOWNLOADING, retry_count=retry_num)
    progress(ds.DOWNLOADING, step="starting", retry=retry_num)
    logger.info("Starting download for '%s' by '%s' (download_id=%s, retry=%d)", track.title, track.artist, download_id, retry_num)

    # Determina storage provider attivo: quello del setting ``storage_provider``
    # (nessun default); senza, ``storage`` è None e il passo di storage
    # fallisce con "no storage provider available" — vedi services/storage.py.
    storage, storage_name = resolve_storage_provider(registry, config.storage_provider)

    with Session(engine) as session:
        # The download is the instance's work, whoever asked for it: it uses
        # the shared accounts of its providers (``services.credentials``).
        auth_manager = auth_manager_for(shared_store(session))

        # Un sync provider che ha già il file vale come "in libreria" anche se
        # nessun job l'ha ancora indicizzato: lo si registra e non si scarica
        # una seconda copia (``services.sync_index``).
        if not force and sync_index.index_track(session, registry, auth_manager, track.internal_id):
            _update_download_status(download_id, status=ds.DOWNLOADED, error_message="already in the library")
            _on_landed(download_id)
            progress(ds.DOWNLOADED)
            return {"status": ds.DOWNLOADED, "detail": "already_in_library"}

        resolver = DownloadResolver(
            registry=registry,
            auth_manager=auth_manager,
            provider_order=config.download_provider_order,
            slots=RedisProviderSlots(
                lease_seconds=config.download_hard_time_limit_seconds,
                wait_seconds=config.provider_slot_wait_seconds,
            ),
            cooldown=RedisProviderCooldown(
                errors=config.provider_cooldown_errors,
                seconds=config.provider_cooldown_seconds,
            ),
        )

        # Nessun downloader accetta gli identificatori che la traccia ha
        # (es. solo l'id di un provider di metadati che nessuno scarica, più
        # un ISRC che nessuno accetta): si cercano gli id degli altri
        # provider prima di tentare, altrimenti la fase per ID non ha nulla
        # da provare.
        if not resolver.accepts_any(track.external_ids):
            progress(ds.DOWNLOADING, step="enriching", retry=retry_num)
            try:
                cross_enrich_track(track, registry, auth_manager)
                logger.info(
                    "Worker enriched '%s': %d external_ids",
                    track.title, len(track.external_ids),
                )
                # Persisti i nuovi ids nel DB per i retry futuri
                from db.track_store import add_identifiers
                add_identifiers(track.internal_id, track.external_ids, session)
                session.commit()
            except Exception as exc:
                logger.warning("Worker enrich failed for '%s': %s", track.title, exc)

        def _on_trying(provider_name: str, method: str) -> None:
            progress(ds.DOWNLOADING, step="downloading", provider_attempting=provider_name,
                     method=method, retry=retry_num)

        storing = _Storing(
            storage=storage, storage_name=storage_name, auth_manager=auth_manager,
            cooldown=RedisProviderCooldown(
                errors=config.provider_cooldown_errors,
                seconds=config.provider_cooldown_seconds,
            ),
        )
        held = held_uploads.find(config.workspace_dir, track.internal_id)
        if held is not None:
            # Already downloaded and verified by an earlier attempt, whose
            # upload failed: only the storage step is done again.
            logger.info("'%s': uploading the file held by an earlier attempt", track.title)
            result = ResolveResult(
                success=True, provider=held.provider, file_name=held.file_name,
                confidence=MatchConfidence(held.confidence) if held.confidence else MatchConfidence.EXACT,
            )
            relative = _store_download(
                result, held.workspace, track, storing, registry, config,
                download_id, retry_num, progress, verified=True,
            )
        else:
            with open_workspaces(config.workspace_dir) as workspaces:
                result = resolver.resolve(track, workspaces.path_for, on_trying=_on_trying)
                if result.success and not result.delegated:
                    relative = _store_download(
                        result, workspaces.for_provider(result.provider), track, storing, registry, config,
                        download_id, retry_num, progress,
                    )
        if result.success and not result.delegated:
            held_uploads.release(config.workspace_dir, track.internal_id)

            # ----- Sync phase -----
            # Lo scan è fire-and-forget: la propagazione del like al sync
            # provider (reconcile_like) converge quando il file è indicizzato.
            sync_name = config.sync_provider
            sync = registry.get_sync_provider(sync_name) if sync_name else None
            if sync:
                try:
                    sync_config = provider_config_from_system_settings(
                        sync_name, SettingsRepository(session).all_system(),
                    )
                    if sync_config.get("mode") == "full":
                        sync.trigger_scan(auth_manager.authenticate(sync))
                        logger.info("Sync scan triggered on '%s'", sync_name)
                except Exception as exc:
                    logger.warning("Sync trigger failed for '%s': %s", sync_name, exc)

    # ----- Delegated to external system -----
    if result.delegated:
        _update_download_status(
            download_id,
            status=ds.DISPATCHED,
            provider_used=result.provider,
            delegation_handle=result.delegated_handle,
        )
        progress(ds.DISPATCHED)
        logger.info(
            "Download dispatched to external system via %s for '%s': %s",
            result.provider, track.title, result.delegated_details,
        )
        # Riconciliazione: se il provider riporta lo stato (poll_delegation), un
        # task di follow-up ne segue il ciclo (downloading→importing→downloaded)
        # con max-age; se non lo riporta, resta dispatched (fire-and-forget).
        # La chain si avvia con il suo lock già preso: il primo tick arriva
        # dopo l'intervallo di poll, e lo sweep nel frattempo non deve
        # crederla orfana e avviarne una seconda.
        if result.delegated_handle:
            from workers.maintenance import start_reconciliation
            start_reconciliation(download_id, config)
        return {"status": ds.DISPATCHED, "provider": result.provider, "details": result.delegated_details}

    if result.success:
        _update_download_status(
            download_id,
            status=ds.DOWNLOADED,
            provider_used=result.provider,
            file_path=relative,
        )
        _register_library_entry(
            track_id=track.internal_id,
            file_path=relative,
            storage_provider=storage_name,
        )
        _on_landed(download_id)
        progress(ds.DOWNLOADED)
        logger.info(
            "Download complete: '%s' via %s (confidence=%s, storage=%s)",
            track.title, result.provider, result.confidence.value, storage_name,
        )
        return {"status": ds.DOWNLOADED, "provider": result.provider, "file_path": relative}

    # Fallimento — ritenta SOLO se l'errore è transitorio (rete, provider in
    # errore, timeout). Un esito definitivo (nessun provider ha la traccia) NON
    # è ritentabile: fallisce all'istante, evitando il retry storm su brani
    # semplicemente non disponibili.
    # L'attesa raddoppia a ogni tentativo fino al tetto, con un po' di
    # casualità (tracce nate insieme, es. un album, non ritentano in blocco).
    max_retries = config.max_retries
    if result.retriable and self.request.retries < max_retries:
        # Un provider in pausa (cool-down) non torna prima della fine della
        # pausa: ritentare prima consumerebbe un tentativo a vuoto.
        delay = max(result.retry_after, retry_delay(
            self.request.retries + 1, config.retry_delay_seconds, random.random(), config.retry_max_delay_seconds,
        ))
        logger.warning(
            "Download failed for '%s', retry %d/%d in %ds",
            track.title, self.request.retries + 1, max_retries, delay,
        )
        raise self.retry(
            exc=RuntimeError(result.error),
            countdown=delay,
            kwargs={"track_data": track_data, "download_id": download_id, "user_id": user_id, "force": force},
        )

    # Un fallimento transitorio che ha esaurito i tentativi resta
    # ``retriable``: lo riprende più tardi il routine dei transitori.
    _update_download_status(download_id, status=ds.FAILED, error_message=result.error, retriable=result.retriable)
    progress(ds.FAILED)
    logger.error("Download permanently failed for '%s': %s", track.title, result.error)
    return {"status": ds.FAILED, "error": result.error}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class _Storing:
    """The storage step of a download: the storage provider, the
    credentials to reach it, and its cool-down (the provider cool-down of the
    resolver, keyed ``storage:<name>``: a storage that keeps timing out
    rests instead of holding the downloads worker at every attempt)."""

    def __init__(self, storage, storage_name: str, auth_manager, cooldown) -> None:
        self.storage = storage
        self.auth_manager = auth_manager
        self.cooldown = cooldown
        self.key = f"storage:{storage_name}"


def _store_download(
    result, workspace, track: Track, storing: _Storing, registry, config,
    download_id: str, retry_num: int, progress=None, *, verified: bool = False,
) -> str | None:
    """
    Porta in libreria il file che il provider ha scritto nella cartella di
    lavoro: verifica di integrità, anti-mislabel, salvataggio sullo storage e
    verifica dopo il salvataggio. Ritorna il path relativo in libreria; su un
    fallimento aggiorna ``result`` (success, error, retriable, retry_after) e
    ritorna None.

    Un fallimento dello STORAGE (upload che solleva, storage in pausa,
    verifica dopo l'upload) non butta il file: lo si tiene
    (``services.held_uploads``) e il prossimo tentativo rifà solo l'upload.
    ``verified``: il file è uno tenuto così, già verificato.
    """
    try:
        downloaded = workspace.file(result.file_name)
    except WorkspaceError as exc:
        return _fail(result, f"invalid download: {exc}", retriable=False)

    if not verified:
        reason = _verify_download(track, downloaded, registry, config)
        if reason:
            return _fail(result, reason, retriable=True)

    storage = storing.storage
    if storage is None:
        return _fail(result, "no storage provider available", retriable=False)

    def hold(error: str, retry_after: float = 0.0) -> None:
        held_uploads.hold(config.workspace_dir, track.internal_id, downloaded, result.provider,
                          result.confidence.value)
        result.retry_after = max(result.retry_after, retry_after)
        return _fail(result, error, retriable=True)

    if storing.cooldown.cooling(storing.key):
        logger.warning("Storage resting after repeated failures: '%s' kept for a later upload", track.title)
        return hold("Storage upload failed: storage resting after repeated failures",
                    storing.cooldown.remaining_seconds(storing.key))

    _update_download_status(download_id, status=ds.IMPORTING)
    if progress is not None:
        progress(ds.IMPORTING, step="importing", retry=retry_num)
    # Misurato prima di store(): lo storage può spostare il file.
    local_size = os.path.getsize(downloaded.path)
    relative_dest = library_path(track, config.track_format, Path(downloaded.name).suffix)
    try:
        storage_session = storing.auth_manager.authenticate(storage)
        stored = storage.store(downloaded, relative_dest, storage_session)
    except Exception as exc:
        storing.cooldown.record(storing.key, ok=False)
        logger.error("Storage failed for '%s' (file kept, upload retried): %s", track.title, exc)
        return hold(f"Storage upload failed: {exc}")
    storing.cooldown.record(storing.key, ok=True)

    ok, reason = _verify_after_upload(storage, storage_session, local_size, stored, track, config)
    if not ok:
        logger.warning("Post-upload verify failed for '%s': %s", track.title, reason)
        try:
            storage.delete(stored, storage_session)
        except Exception as exc:  # noqa: BLE001 — best effort cleanup
            logger.warning("Could not delete corrupt stored file '%s': %s", stored, exc)
        if Path(downloaded.path).exists():
            return hold(f"post-upload verify failed: {reason}")
        return _fail(result, f"post-upload verify failed: {reason}", retriable=True)
    return stored


def _verify_download(track: Track, downloaded, registry, config) -> str | None:
    """Why the downloaded file must not enter the library (corrupt,
    truncated, another song), None when it may."""
    # Un file corrotto o troncato non entra mai in libreria: si ritenta.
    if config.verify_integrity_enabled:
        vr = verify_audio(
            downloaded.path, track.duration_ms,
            tolerance_s=config.verify_duration_tolerance_seconds,
        )
        if not vr.ok:
            logger.warning("Integrity check failed for '%s': %s", track.title, vr.reason)
            return f"corrupt download: {vr.reason}"

    # Verifica che l'audio corrisponda davvero a titolo/artista richiesti.
    # Inerte se nessun IdentificationProvider è disponibile.
    if _should_run_anti_mislabel(config):
        ok, reason = _check_mislabel(track, downloaded, registry, config)
        if not ok:
            logger.warning("Anti-mislabel scarta '%s': %s", track.title, reason)
            return reason
    return None


def _fail(result, error: str | None, *, retriable: bool) -> None:
    result.success = False
    result.error = error
    result.retriable = retriable
    return None


def _should_run_anti_mislabel(config) -> bool:
    """Il toggle GLOBALE ``identification_enabled`` va controllato QUI (il
    call site generico), non dentro un provider: ``is_available()`` di un
    provider risponde solo a "può funzionare?" (libreria + config), mai a
    "il sistema deve fare identificazione?" — se dipendesse dal toggle,
    accendere ``identification_enabled`` (che a sua volta richiede un
    provider usable) sarebbe impossibile finché il toggle non è già acceso.
    Nessun nome di provider: solo i due flag generici dichiarati nel
    catalogo."""
    return bool(config.anti_mislabel_enabled and config.identification_enabled)


def _check_mislabel(track, audio, registry, config) -> tuple[bool, str | None]:
    """
    Identifica l'audio via fingerprint e verifica che corrisponda a
    titolo/artista richiesti. Ritorna (ok, reason). Conservativo: interviene
    solo su un match confidente (filtrato dalla soglia generica, vedi
    ``services.identification.identify_confidently``) chiaramente diverso; in
    ogni dubbio/errore lascia passare. No-op se nessun provider disponibile.
    Il gate sul toggle globale ``identification_enabled`` vive nel chiamante
    (``_should_run_anti_mislabel``): questa funzione presume di essere
    chiamata solo quando ha senso provarci, e resta quindi semplicemente
    "trova un provider usable e chiedigli di identificare", provider-agnostica.
    """
    provider = None
    for p in registry.all_identification():
        try:
            if p.is_available():
                provider = p
                break
        except Exception:  # noqa: BLE001
            continue
    if provider is None:
        return True, None

    try:
        identity = identify_confidently(provider, audio, config)
    except Exception:  # noqa: BLE001 — non bloccare il download su errore identificazione
        return True, None
    if identity is None:
        return True, None  # sotto soglia / non identificato → non affermare nulla

    from musizerr_sdk.text import normalize
    want = normalize(track.title or "")
    got = normalize(identity.title or "")
    if want and got and want != got and want not in got and got not in want:
        return False, (
            f"mislabel: l'audio è '{identity.title}' non '{track.title}' "
            f"(score {identity.score:.2f})"
        )
    return True, None


def _verify_after_upload(storage, session, local_size, remote_rel, track, config) -> tuple[bool, str | None]:
    """
    Verifica il file DOPO il salvataggio su storage. Livello da
    ``config.verify_after_upload``: ``off`` | ``size`` | ``full``.
    Ritorna ``(ok, reason)``. Agnostico: usa solo il contratto StorageProvider.
    """
    level = config.verify_after_upload
    if level == "off":
        return True, None

    if level == "size":
        remote_size = storage.size(remote_rel, session)
        if remote_size is None:
            return True, None  # provider senza size() → skip best-effort
        if remote_size != local_size:
            return False, f"size mismatch: remote {remote_size} vs local {local_size} bytes"
        return True, None

    if level == "full":
        with open_workspace(config.workspace_dir, storage.name) as workspace:
            try:
                streamed = storage.stream(remote_rel, str(workspace), session)
                copy = workspace.local_file(streamed)
            except Exception as exc:  # noqa: BLE001
                return False, f"stream-back failed: {exc}"
            if copy is None:
                return True, None  # il provider rende solo un URL: niente da rileggere qui
            vr = verify_audio(
                copy, track.duration_ms,
                tolerance_s=config.verify_duration_tolerance_seconds,
            )
        return (vr.ok, vr.reason)

    return True, None


def _update_download_status(
    download_id: str | None,
    status: str,
    provider_used: str | None = None,
    file_path: str | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    retry_count: int | None = None,
    delegation_handle: str | None = None,
    retriable: bool | None = None,
) -> None:
    """Scrive lo stato di un download. ``completed_at`` c'è se e solo se lo
    stato è terminale: a ``completed_at`` dato (o adesso) su un terminale,
    vuoto su ogni altro stato."""
    if not download_id:
        return
    with Session(engine) as session:
        record = session.get(Download, download_id)
        if not record:
            return
        record.status = status
        if provider_used is not None:
            record.provider_used = provider_used
        if delegation_handle is not None:
            record.delegation_handle = delegation_handle
        if file_path is not None:
            record.file_path = file_path
        if error_message is not None:
            record.error_message = error_message
        if status in ds.TERMINAL:
            record.completed_at = completed_at or record.completed_at or datetime.now(timezone.utc)
        else:
            record.completed_at = None
        if retriable is not None:
            record.retriable = retriable
        if retry_count is not None:
            record.retry_count = retry_count
        if status == ds.DOWNLOADING and record.started_at is None:
            record.started_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()


def _in_library(track_id: str | None, registry) -> bool:
    if not track_id:
        return False
    with Session(engine) as session:
        return track_id in in_library(session, [track_id], sync_id_types(registry))


def _on_landed(download_id: str) -> None:
    """La traccia è in libreria: diventa un preferito di chiunque l'abbia
    chiesta con il preferito (anche chi si è agganciato alla richiesta), e
    il like parte verso il sync provider (``services.favorites``)."""
    from services.favorites import apply_intents

    with Session(engine) as session:
        dl = session.get(Download, download_id)
        if dl:
            try:
                apply_intents(session, dl.track_id)
            except Exception as exc:  # noqa: BLE001 — il download è arrivato comunque; lo sweep ritenta
                logger.warning("Favorite intents on '%s' not applied yet: %s", dl.track_id, exc)


def _register_library_entry(
    track_id: str,
    file_path: str | None,
    storage_provider: str,
) -> None:
    if not file_path:
        return
    suffix = Path(file_path).suffix.lstrip(".")
    with Session(engine) as session:
        # Re-download of an already-tracked file (retry, manual re-queue,
        # storage backfill) refreshes the link to the current track + format
        # on the row it has (unique file_path), and the file is there again.
        upsert(
            session,
            LibraryEntry(
                track_id=track_id,
                file_path=file_path,
                file_format=suffix,
                source="download",
                storage_provider=storage_provider,
            ),
            conflict=("file_path",),
            update=("track_id", "file_format", "storage_provider", "missing_since"),
        )
        session.commit()


# ------------------------------------------------------------------
# Library sync — saved/liked tracks
# ------------------------------------------------------------------


def _job_session() -> Session:
    """The session of a library job's progress writes: short, its own."""
    return Session(engine)


@celery.task(name="workers.tasks.sync_saved_tracks_for_user")
def sync_saved_tracks_for_user(user_id: str, run_id: str | None = None) -> dict:
    """Imports the user's likes from every provider that has a user library,
    as the run ``run_id`` (``services/library_jobs.py``): phase ``fetching``
    then ``saving`` per provider, one entry per provider. The run fails only
    when every provider failed."""
    from db.models import User

    reporter = library_jobs.JobReporter.claim(
        user_id, job_store.KIND_LIBRARY_SYNC, run_id, session_factory=_job_session,
    )
    if reporter is None:
        return {"status": "skipped", "detail": "run not queued"}

    registry = get_registry()
    try:
        with Session(engine) as session:
            user = session.get(User, user_id)
            if not user:
                reporter.finish(job_store.STATUS_FAILED, error_code=library_jobs.ACCOUNT_REQUIRED)
                return {"status": "error", "detail": "user not found"}
            providers = [p for p in registry.all_metadata() if p.supports_user_library()]
            if providers:
                reporter.set_providers(p.name for p in providers)
                outcome = _sync_user_library(session, user, providers, reporter, registry)
            else:
                # Nothing could ever be imported: say so, never "completed, 0 tracks".
                reporter.finish(job_store.STATUS_FAILED, error_code=library_jobs.NO_LIBRARY_PROVIDER)
                outcome = {"status": "failed", "total_synced": 0}
    except Exception as exc:  # noqa: BLE001 — the run must end, whatever happened
        logger.exception("Library sync failed for %s", user_id)
        reporter.finish(job_store.STATUS_FAILED, error_code=library_jobs.JOB_FAILED, error_detail=str(exc))
        return {"status": "error", "detail": str(exc)}

    # Likes just imported may name tracks of the library: the library is
    # brought up to date from the sync providers, then the like matching
    # follows (``sync_library_index`` → ``match_likes``).
    routines.start_routine(SYNC_LIBRARY_INDEX)
    return outcome


def _sync_user_library(session, user, providers, reporter, registry) -> dict:
    # The likes are the user's own: their own account only, never a shared
    # one (and never an app-only token): ``own_store`` (with authentication off
    # the user is the instance and its own account is the shared one, see
    # ``services.credentials``). Checked before authenticating, so a user
    # without one is "not connected", not an error.
    store = own_store(session, user.id)
    auth_manager = auth_manager_for(store)
    total_synced = 0
    failures: list[dict] = []

    for provider in providers:
        if reporter.cancelled:
            break
        reporter.progress(library_jobs.PHASE_FETCHING, provider=provider.name, force=True)
        try:
            if not has_own_account(store, provider):
                raise AuthError(f"no account of the user on {provider.name}")
            auth_session = auth_manager.authenticate(provider)
            tracks = _fetch_all_saved_tracks(
                provider, auth_session,
                on_page=lambda fetched, name=provider.name: reporter.progress(
                    library_jobs.PHASE_FETCHING, provider=name, processed=fetched),
            )
            reporter.progress(library_jobs.PHASE_SAVING, provider=provider.name, processed=0, total=len(tracks))
            synced = _persist_saved_tracks(
                tracks, user.id, provider.name, session, registry,
                on_progress=lambda done, total, name=provider.name: reporter.progress(
                    library_jobs.PHASE_SAVING, provider=name, processed=done, total=total),
            )
            total_synced += synced
            reporter.update_provider(provider.name, status=job_store.STATUS_COMPLETED, count=synced)
        except AuthError:
            code = library_jobs.PROVIDER_NOT_CONNECTED
            reporter.fail_provider(provider.name, code, library_jobs.MESSAGES[code])
            failures.append({"error_code": code, "error_message": library_jobs.MESSAGES[code], "detail": None})
        except Exception as exc:  # noqa: BLE001 — one provider's failure never stops the others
            logger.warning("Library sync failed for %s: %s", provider.name, exc)
            session.rollback()
            body = public_error(exc, "metadata_provider", show_detail=True)
            reporter.fail_provider(provider.name, body["error_code"], body["error_message"], body["error_detail"])
            failures.append({**body, "detail": body["error_detail"]})
        reporter.flush()

    result = {"tracks_synced": total_synced}
    if providers and len(failures) == len(providers):
        first = failures[0]
        reporter.finish(job_store.STATUS_FAILED, result=result, error_code=first["error_code"],
                        error_message=first["error_message"], error_detail=first["detail"])
        return {"status": "failed", "total_synced": total_synced}
    reporter.finish(job_store.STATUS_COMPLETED, result=result)
    return {"status": "done", "total_synced": total_synced}


@celery.task(name="workers.tasks.sync_library_index")
def sync_library_index_task(progress=None) -> dict:
    """Routine ``sync_library_index``: porta il database in linea con le
    librerie di ogni sync provider (``services.sync_index``) — la presenza
    sul sync provider si scrive qui, mai in una lettura. Il risultato ha un
    resoconto per provider; un provider che non risponde (o un elenco vuoto
    o parziale) fa fallire la run con il suo codice d'errore. ``progress``:
    il reporter della run (``services.routines.RunProgress``)."""
    registry = get_registry()
    with Session(engine) as session:
        reports = sync_index.index_libraries(
            session, registry, auth_manager_for(shared_store(session)), progress=progress,
        )
    return sync_index.index_result(reports)


@celery.task(name="workers.tasks.sync_playlists")
def sync_playlists_task(progress=None) -> dict:
    """Routine ``sync_playlists``: le playlist degli utenti sul loro account
    di ogni sync provider che le tiene (``services.playlist_sync``). Il
    risultato ha un resoconto per provider; un provider che fallisce fa
    fallire la run col suo codice. ``progress``: il reporter della run."""
    registry = get_registry()
    with Session(engine) as session:
        reports = playlist_sync.push_all(session, registry, progress=progress)
    return playlist_sync.push_result(reports)


@celery.task(name="workers.tasks.refresh_followed_playlists")
def refresh_followed_playlists_task(progress=None) -> dict:
    """Routine ``refresh_followed_playlists``: ogni playlist seguita si
    confronta con la sua sorgente (``services.playlist_monitor``); le tracce
    nuove si scaricano come chiede la policy di approvazione. Una sorgente
    illeggibile fa fallire la run col suo codice. ``progress``: il reporter."""
    registry = get_registry()
    with Session(engine) as session:
        return playlist_monitor.refresh_all(session, registry, get_config(), progress=progress)


@celery.task(name="workers.tasks.match_likes")
def match_likes_task(user_id: str | None = None) -> dict:
    """Like matching (``services.favorites.match_likes``): i like importati
    dagli altri provider che corrispondono a tracce in libreria diventano
    preferiti, senza scaricare nulla. Segue il routine ``sync_library_index``
    (parte alla fine di ogni sua run) e abbina con quello che il database sa:
    il risultato dice com'è finito l'ultimo aggiornamento della libreria
    (``library_index``), così un aggiornamento fallito non resta nascosto.
    ``user_id``: solo i messaggi accodati dalla versione precedente (dopo la
    sincronizzazione dei like di un utente) lo portano ancora."""
    from services.favorites import match_likes

    registry = get_registry()
    with Session(engine) as session:
        result = dict(match_likes(session, registry, user_id=user_id))
        result["library_index"] = sync_index.last_index(session)
        return result


@celery.task(name="workers.tasks.sync_all_users_saved_tracks")
def sync_all_users_saved_tracks() -> dict:
    """The periodic library sync: a run for every user the host acts for
    (``services.credentials.account_holders``: the active users, or the
    instance itself with authentication off) with an account connected on a
    provider that has a user library, through the same idempotent start as
    "Sync now" (a user whose sync is already queued or running is left
    alone). A user with no such account is skipped: a run for them could only
    fail as ``provider_not_connected``, every period."""
    registry = get_registry()
    providers = [p for p in registry.all_metadata() if p.supports_user_library()]
    started = 0
    skipped = 0
    with Session(engine) as session:
        users = account_holders(session)
        for u in users:
            store = own_store(session, u.id)
            if not any(has_own_account(store, p) for p in providers):
                skipped += 1
                continue
            try:
                _, created = library_jobs.start(session, u.id, job_store.KIND_LIBRARY_SYNC)
            except library_jobs.QueueUnavailable:
                continue
            started += int(created)

    return {"dispatched": started, "users": len(users), "skipped_not_connected": skipped}


def _fetch_all_saved_tracks(provider, auth_session, page_size=50, on_page=None):
    """Every saved track of ``provider``, page by page: the provider decides
    where its library ends (``MetadataProvider.iter_saved_tracks``)."""
    all_tracks = []
    for batch in provider.iter_saved_tracks(auth_session, page_size):
        all_tracks.extend(batch)
        if on_page is not None:
            on_page(len(all_tracks))
    return all_tracks


_PERSIST_BATCH = 100


def _persist_saved_tracks(tracks, user_id, provider_name, session, registry, on_progress=None) -> int:
    """Resolves each track onto its canonical TrackRecord (merging metadata
    across providers via find_or_create_track) and links it to the user's
    library, instead of owning a private per-saved-track metadata blob.
    A like imported from the sync provider carries its sync id too (one
    identity space, ``services.track_identity``): it resolves to the library
    record of the same file instead of becoming a twin.
    ``on_progress(done, total)`` is called right after each batch commit,
    when this session holds no write lock."""
    from core import urls
    from db.models import UserSavedTrack, UserSavedTrackProvider
    from db.track_store import find_or_create_track
    from services.track_identity import with_sync_aliases

    synced = 0
    # Aware UTC, like every datetime read back from the database
    # (db.types.UTCDateTime); a provider's saved_at is read as UTC (as_utc).
    now = datetime.now(timezone.utc)

    for index, track in enumerate(tracks, start=1):
        # Una libreria grande in un'unica transazione terrebbe il lock di
        # scrittura per tutto il tempo: si committa a lotti.
        if index % _PERSIST_BATCH == 0:
            session.commit()
            if on_progress is not None:
                on_progress(index - 1, len(tracks))
        record = find_or_create_track(with_sync_aliases(track, registry), session)

        existing = session.exec(
            select(UserSavedTrack).where(
                UserSavedTrack.user_id == user_id,
                UserSavedTrack.track_id == record.internal_id,
            )
        ).first()

        saved_at = as_utc(track.saved_at)
        if existing:
            if saved_at and saved_at < as_utc(existing.saved_at):
                existing.saved_at = saved_at
                session.add(existing)
            saved_track_id = existing.id
        else:
            row = UserSavedTrack(
                user_id=user_id,
                track_id=record.internal_id,
                saved_at=saved_at or now,
            )
            session.add(row)
            session.flush()
            saved_track_id = row.id
            synced += 1

        prov_row = session.exec(
            select(UserSavedTrackProvider).where(
                UserSavedTrackProvider.saved_track_id == saved_track_id,
                UserSavedTrackProvider.provider == provider_name,
            )
        ).first()

        if prov_row:
            prov_row.provider_track_id = track.provider_id or ""
            prov_row.added_at = saved_at
            prov_row.synced_at = now
            prov_row.stream_url = urls.to_host_ref(track.stream_url)
            session.add(prov_row)
        else:
            session.add(UserSavedTrackProvider(
                saved_track_id=saved_track_id,
                provider=provider_name,
                provider_track_id=track.provider_id or "",
                added_at=saved_at,
                synced_at=now,
                stream_url=urls.to_host_ref(track.stream_url),
            ))

    session.commit()
    if on_progress is not None:
        on_progress(len(tracks), len(tracks))
    return synced


# ------------------------------------------------------------------
# Library download sync — download missing tracks
# ------------------------------------------------------------------


@celery.task(name="workers.tasks.library_download_sync")
def library_download_sync(user_id: str, time_range: str = "all", run_id: str | None = None) -> dict:
    """Queues the download of the user's likes that are not in the library
    and no sync provider has, as the run ``run_id``
    (``services/library_jobs.py``): phase ``preparing``, then ``checking``
    with processed/total."""
    reporter = library_jobs.JobReporter.claim(
        user_id, job_store.KIND_DOWNLOAD_SYNC, run_id, session_factory=_job_session,
    )
    if reporter is None:
        return {"status": "skipped", "detail": "run not queued"}
    try:
        return _download_missing_likes(user_id, time_range, reporter)
    except Exception as exc:  # noqa: BLE001 — the run must end, whatever happened
        logger.exception("Library download sync failed for %s", user_id)
        reporter.finish(job_store.STATUS_FAILED, error_code=library_jobs.JOB_FAILED, error_detail=str(exc))
        return {"status": "error", "detail": str(exc)}


def _download_missing_likes(user_id: str, time_range: str, reporter) -> dict:
    from datetime import timedelta
    from db.models import User, UserSavedTrack, UserSavedTrackProvider, TrackRecord
    from db.track_store import load_track

    registry = get_registry()
    reporter.progress(library_jobs.PHASE_PREPARING, force=True)

    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            reporter.finish(job_store.STATUS_FAILED, error_code=library_jobs.ACCOUNT_REQUIRED)
            return {"status": "error", "detail": "user not found"}

        # Un like già collegato a una traccia della libreria non manca.
        query = select(UserSavedTrack).where(
            UserSavedTrack.user_id == user_id, UserSavedTrack.library_track_id.is_(None),
        )

        if time_range in ("1m", "3m"):
            days = 30 if time_range == "1m" else 90
            # The column stores naive UTC text and converts the bound value
            # to it (db.types.UTCDateTime): an aware cutoff compares right.
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            query = query.where(
                UserSavedTrack.id.in_(
                    select(UserSavedTrackProvider.saved_track_id).where(
                        UserSavedTrackProvider.added_at >= cutoff
                    )
                )
            )

        rows = session.exec(query.order_by(UserSavedTrack.saved_at.desc())).all()
        total = len(rows)
        checked = 0
        queued = 0
        errors = 0
        skipped_removed = 0
        pending: dict | None = None

        if rows:
            track_records = session.exec(
                select(TrackRecord).where(TrackRecord.internal_id.in_([r.track_id for r in rows]))
            ).all()
            track_map = {t.internal_id: t for t in track_records}

            # Sync providers are configured system-wide, not per-user —
            # match the policy in services/credentials.py's non-metadata branch.
            # (A user-scoped store here previously made non-admin users silently fail
            # to authenticate against sync providers, since their credentials live
            # in the system-wide row, not a per-user one.)
            auth_manager = auth_manager_for(shared_store(session))

            sync_names = {sync.name for sync in registry.all_sync()}
            cached_rows = session.exec(
                select(UserSavedTrackProvider).where(
                    UserSavedTrackProvider.saved_track_id.in_([r.id for r in rows]),
                    UserSavedTrackProvider.stream_url.isnot(None),
                    UserSavedTrackProvider.provider.in_(sync_names),
                )
            ).all() if sync_names else []
            cached_available = {pr.saved_track_id for pr in cached_rows}

            reporter.progress(library_jobs.PHASE_CHECKING, processed=0, total=total)

            wanted = []
            for row in rows:
                if reporter.cancelled:
                    break
                checked += 1
                record = track_map.get(row.track_id)
                if row.id not in cached_available and record and record.track_data:
                    if favorites.is_removed(session, user_id, record.internal_id):
                        # A favorite the user took off stays off: a like that is
                        # still on the other provider does not bring it back.
                        skipped_removed += 1
                    # A sync provider that has the file makes it a track of the
                    # library (recorded now, like the likes matching does).
                    elif not sync_index.index_track(session, registry, auth_manager, record.internal_id):
                        wanted.append(load_track(session, record))

                reporter.progress(library_jobs.PHASE_CHECKING, processed=checked, total=total)

            if wanted:
                # The likes download is a request like any other: for a user the
                # approval policy governs, it may wait for an admin instead of
                # queueing anything (``services/download_requests.py``). The
                # user's own likes arrive as their favorites.
                outcome = request_download(
                    session, user, liked_elsewhere_spec(wanted), favorite_for=user_id,
                    config=get_config(), registry=registry, auth_manager=auth_manager,
                )
                if outcome.pending:
                    pending = {
                        "request_id": outcome.request.id,
                        "needed_tracks": outcome.request.needed_tracks,
                        "reason": outcome.request.reason,
                    }
                else:
                    queued = outcome.execution.batch.queued
                    errors = outcome.execution.batch.errors

    result = {"total": total, "checked": checked, "queued": queued, "errors": errors,
              "skipped_removed": skipped_removed}
    if pending is not None:
        # Nothing was queued: the request waits for an admin.
        result["pending_approval"] = pending
    reporter.finish(job_store.STATUS_COMPLETED, result=result)
    return {"status": "done", **result}


# ------------------------------------------------------------------
# Transient failures, retried later
# ------------------------------------------------------------------


@celery.task(name="workers.tasks.retry_transient_failures")
def retry_transient_failures() -> dict:
    """Routine: rimette in coda, poche per volta, le tracce il cui ultimo
    download è fallito per un errore transitorio
    (``services.downloads.retry_transient_failures``)."""
    with Session(engine) as session:
        return download_service.retry_transient_failures(session, get_registry(), get_config())
