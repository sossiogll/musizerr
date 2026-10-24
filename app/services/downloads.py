"""Logica di download condivisa tra track, album, playlist, artist, retry e
il routine dei fallimenti transitori.

Un'unica porta per ogni richiesta (``_request``): una traccia già in
libreria non riceve mai un nuovo download (salvo ``force``, solo admin: lo
decide la rotta), una traccia con una richiesta aperta vi si aggancia, e il
task riceve la traccia CANONICA con tutti i suoi identificatori, non solo
quelli della riga da cui è partita la richiesta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from services.config_manager import get_config
from core import download_states as ds
from musizerr_sdk.errors import DownloadDelegated
from musizerr_sdk.models import Album, Track
from core.serialization import serialize_track
from core.utils import provider_sort_key
from db.download_store import add_follower, find_open_download, mark_unqueued
from db.library_store import in_library
from db.locks import xact_lock
from db.models import Download, TrackIdentifierRecord, TrackRecord
from db.track_store import find_or_create_track
from services.track_identity import with_sync_aliases
from services import favorites, provider_order
from services.stored_tracks import public_track_dict
from services.track_state import file_state_of, sync_id_types
from workers.celery_app import celery

logger = logging.getLogger(__name__)

# Esito di una richiesta di download.
QUEUED = "queued"                          # nuovo download in coda
ATTACHED = "attached"                      # agganciata alla richiesta già aperta
ALREADY_IN_LIBRARY = "already_in_library"  # nessun download: la traccia c'è già


@dataclass(frozen=True)
class EnqueueResult:
    status: str
    # La traccia canonica nel database (per la UI: cuore, stream, stato).
    library_id: str
    download_id: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "download_id": self.download_id, "library_id": self.library_id}


def attach_to_open_download(session: Session, track_id: str, user_id: str | None) -> str | None:
    """
    Se la traccia ha già una richiesta in corso, non ne crea una seconda (la
    libreria è unica: due richieste parallele producono due copie dello stesso
    brano). Ritorna l'id della richiesta esistente, o None se non ce n'è.

    Chi si aggancia alla richiesta di un altro utente non perde la richiesta
    dalla propria cronologia (diventa un DownloadFollower); il suo preferito
    all'arrivo lo registra chi chiama (``favorite_for``).
    """
    open_dl = find_open_download(session, track_id)
    if open_dl is None:
        return None
    if user_id and user_id != open_dl.requested_by:
        add_follower(session, open_dl.id, user_id)
    return open_dl.id


def enqueue_track(
    track: Track, session: Session, user_id: str | None = None, favorite_for: str | None = None, registry=None,
    force: bool = False,
) -> EnqueueResult:
    """
    Risolve la Track sulla TrackRecord canonica (creandola o mergiandola se già
    esiste, via find_or_create_track) e ne chiede il download (``_request``).

    Con ``favorite_for`` la traccia diventa un preferito di quell'utente
    quando arriva in libreria — subito, se c'è già
    (``services.favorites.register_intent``); la decisione è di
    ``services.capabilities.favorite_on_arrival``.

    A track of the sync provider is resolved with its sync id too (one
    identity space, ``services.track_identity``): it is the library record.
    """
    record = find_or_create_track(with_sync_aliases(track, registry) if registry is not None else track, session)
    return _request(session, record, user_id, favorite_for, registry, force)


def retry_track(
    session: Session, record: TrackRecord, user_id: str | None, favorite_for: str | None, registry,
    force: bool = False,
) -> EnqueueResult:
    """Un nuovo tentativo per una traccia già nel database (retry dalla
    cronologia, routine dei fallimenti transitori): stessa porta di
    ``enqueue_track``."""
    return _request(session, record, user_id, favorite_for, registry, force)


def _request(session: Session, record: TrackRecord, user_id, favorite_for, registry, force: bool) -> EnqueueResult:
    track_id = record.internal_id
    if favorite_for:
        favorites.register_intent(session, favorite_for, track_id, registry)

    # One request per track at a time: of two requests arriving together the
    # second finds the first's open download (``attached``) instead of adding
    # another one. Held until the download is committed.
    xact_lock(session, f"download-request:{track_id}")
    if not force and track_id in in_library(session, [track_id], _sync_types(registry)):
        return EnqueueResult(ALREADY_IN_LIBRARY, track_id)

    existing = attach_to_open_download(session, track_id, user_id)
    if existing:
        return EnqueueResult(ATTACHED, track_id, existing)

    dl = Download(track_id=track_id, requested_by=user_id, status=ds.PENDING)
    session.add(dl)
    session.commit()
    session.refresh(dl)

    try:
        queue_download(track_data=task_track_data(session, record), download_id=dl.id, user_id=user_id, force=force)
    except Exception as exc:  # noqa: BLE001 — whatever stops the message (broker down, ...)
        # No worker will ever see this download: it must not stay an open
        # request that others attach to. It ends as a retriable transient
        # failure the routine queues again (``db.download_store.mark_unqueued``).
        logger.warning("Download %s of track %s could not be queued: %s", dl.id, track_id, exc)
        session.rollback()
        mark_unqueued(session, dl.id, str(exc) or type(exc).__name__, datetime.now(timezone.utc))
    return EnqueueResult(QUEUED, track_id, dl.id)


def _sync_types(registry) -> list[str]:
    return sync_id_types(registry) if registry is not None else []


def task_track_data(session: Session, record: TrackRecord) -> dict:
    """La traccia che riceve il task: quella canonica (metadati mergiati da
    ogni fonte) con TUTTI i suoi identificatori, come stanno in
    ``track_identifiers`` (arricchimento del worker, sync, scansione)."""
    id_records = session.exec(
        select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == record.internal_id)
    ).all()
    return public_track_dict(record.internal_id, record, id_records)


@dataclass(frozen=True)
class BatchResult:
    """Esito di una raccolta di tracce passata dalla porta, una per una."""

    results: list[EnqueueResult]
    # Le tracce che non hanno né un download nuovo né uno a cui agganciarsi:
    # già in libreria, lasciate fuori da ``only_missing``, o fallite (``errors``).
    skipped: int
    # Tracce la cui richiesta ha sollevato un errore (registrato e saltato).
    errors: int = 0

    @property
    def download_ids(self) -> list[str]:
        """I download delle tracce: nuovi e a cui ci si è agganciati."""
        return [r.download_id for r in self.results if r.download_id]

    @property
    def queued(self) -> int:
        """I download NUOVI messi in coda."""
        return sum(1 for r in self.results if r.status == QUEUED)


def enqueue_tracks(
    tracks: list[Track], session: Session, registry, user_id: str | None = None, only_missing: bool = False,
    favorite_for: str | None = None,
) -> BatchResult:
    """
    Mette in coda i brani di una raccolta (album, playlist, artista, i like
    di altrove). Quelli già in libreria non si riscaricano mai
    (``enqueue_track``); con ``only_missing`` si saltano anche quelli in
    arrivo (lo stato file di ``services.track_state``, lo stesso che conta la
    copertura della raccolta: si scaricano solo i "mancanti", i falliti
    compresi).

    Una traccia che solleva un errore non ferma le altre: è registrata e
    contata (``errors``). Se NESSUNA traccia è passata, l'errore sale: un
    guasto di sistema (database, broker) non diventa un esito vuoto.
    """
    if only_missing:
        states = file_state_of([serialize_track(track) for track in tracks], session, registry)
        wanted = [
            track for track, state in zip(tracks, states)
            if state is None or state == ds.FAILED
        ]
    else:
        wanted = list(tracks)
    results: list[EnqueueResult] = []
    failure: Exception | None = None
    errors = 0
    for track in wanted:
        try:
            results.append(enqueue_track(track, session, user_id=user_id, favorite_for=favorite_for, registry=registry))
        except Exception as exc:  # noqa: BLE001 — una traccia non ferma la raccolta
            logger.warning("Failed to enqueue '%s': %s", track.title, exc)
            session.rollback()
            errors += 1
            failure = failure or exc
    if failure is not None and not results:
        raise failure
    with_download = sum(1 for r in results if r.download_id)
    return BatchResult(results, skipped=len(tracks) - with_download, errors=errors)


@dataclass(frozen=True)
class AlbumResult:
    """Esito del download di un album: consegnato a un sistema esterno
    (``delegated``), oppure le sue tracce passate dalla porta (``batch``)."""

    delegated: DownloadDelegated | None = None
    batch: BatchResult | None = None


def download_album(
    album: Album, session: Session, registry, auth_manager, config, user_id: str | None = None,
    only_missing: bool = False, favorite_for: str | None = None,
) -> AlbumResult:
    """
    Scarica un album. Il dispaccio a livello di album (``dispatch_album``, es.
    un gestore di libreria esterno) anticipa il risolutore per traccia solo se
    il provider di download in cima all'ordine configurato lo offre e lo
    accetta (``DownloadDelegated``): altrimenti un provider a priorità più
    bassa vincerebbe sempre il download d'album, perché ``dispatch_album`` è
    una presa a tutto-o-niente — il risolutore per traccia rispetta già
    l'ordine dei provider di download (``services/provider_order``) e ripiega per traccia sui provider capaci di
    dispaccio. Se il dispaccio non c'è o non riesce, ogni traccia passa dalla
    porta (``enqueue_tracks``).
    """
    download_order = provider_order.effective_download_order(registry, config, session)
    download_providers = sorted(
        registry.all_download(),
        key=lambda p: provider_sort_key(p.name, download_order),
    )
    if download_providers:
        top = download_providers[0]
        try:
            top.dispatch_album(album, auth_manager.authenticate(top))
        except DownloadDelegated as exc:
            return AlbumResult(delegated=exc)
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001 — il dispaccio è un'ottimizzazione: si ripiega sulle tracce
            logger.info("Album dispatch by '%s' did not go through: %s", top.name, exc)
    batch = enqueue_tracks(
        album.tracks, session, registry, user_id=user_id, only_missing=only_missing, favorite_for=favorite_for,
    )
    return AlbumResult(batch=batch)


def queue_download(track_data: dict, download_id: str, user_id: str | None = None, force: bool = False) -> None:
    """
    Unico punto da cui si accoda ``download_task``. I limiti di tempo si leggono
    ora, non all'import: una modifica dalla UI vale dal prossimo download.
    Celery li riporta da solo sui retry (``Task.signature_from_request``).
    """
    config = get_config()
    celery.send_task(
        "workers.tasks.download_task",
        kwargs={
            "track_data": track_data,
            "download_id": download_id,
            "user_id": user_id,
            "force": force,
        },
        soft_time_limit=config.download_soft_time_limit_seconds,
        time_limit=config.download_hard_time_limit_seconds,
    )


def retry_transient_failures(session: Session, registry, config) -> dict:
    """
    Il routine dei fallimenti transitori: rimette in coda le tracce il cui
    ultimo download è fallito per un errore temporaneo (``retriable``; anche
    quelle fallite prima che lo si registrasse, ``None``) da almeno
    ``transient_retry_after_hours``, meno di ``transient_retry_max_rounds``
    volte, fuori dalla libreria e senza una richiesta aperta. Al più
    ``transient_retry_batch_size`` per giro, le più vecchie prima: un
    fallimento di massa torna ai provider un poco alla volta.
    """
    after = timedelta(hours=int(config.get("transient_retry_after_hours")))
    max_rounds = int(config.get("transient_retry_max_rounds"))
    batch = int(config.get("transient_retry_batch_size"))
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - after

    attempts: dict[str, list[Download]] = {}
    failed_tracks = select(Download.track_id).where(Download.status == ds.FAILED)
    for dl in session.exec(select(Download).where(Download.track_id.in_(failed_tracks))).all():
        attempts.setdefault(dl.track_id, []).append(dl)

    owned = in_library(session, list(attempts), _sync_types(registry))
    due: list[tuple[datetime, Download]] = []
    for track_id, rows in attempts.items():
        if track_id in owned or any(r.status != ds.FAILED for r in rows) or len(rows) >= max_rounds:
            continue
        latest = max(rows, key=lambda r: _naive(r.completed_at or r.started_at) or datetime.min)
        finished = _naive(latest.completed_at or latest.started_at)
        if latest.retriable is False or finished is None or finished > cutoff:
            continue
        due.append((finished, latest))

    queued = 0
    for _, latest in sorted(due, key=lambda item: item[0])[:batch]:
        record = session.get(TrackRecord, latest.track_id)
        if record is None:
            continue
        if retry_track(session, record, latest.requested_by, None, registry).status == QUEUED:
            queued += 1
    return {"candidates": len(due), "queued": queued}


def _naive(value: datetime | None) -> datetime | None:
    """UTC senza tzinfo, come le date che SQLite restituisce."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
