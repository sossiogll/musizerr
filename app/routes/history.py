from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select, func

from core import download_states as _ds
from core.utc import as_utc, utc_iso
from db.download_store import (
    aggregate_status,
    ranks_of_phase,
    status_summary,
    track_status_ranks,
    visible_to_user_clause,
)
from db.engine import read_snapshot
from db.expressions import descending
from db.library_store import in_library
from db.models import Download, TrackRecord, TrackIdentifierRecord, User
from services.download_requests import may_retry
from services.downloads import QUEUED, retry_track
from services.enrichment import enrich_dicts_with_stream_url
from services.errors import public_error
from services.stored_tracks import public_track_dict
from services.track_state import annotate_tracks, sync_id_types
from routes.deps import (
    AuthManagerDep,
    ConfigDep,
    CurrentUserDep,
    DownloaderDep,
    RegistryDep,
    SessionDep,
    ViewerDep,
)
from routes._pagination import paginated_response

DownloadSort = Literal["recent", "title", "artist"]
DownloadPhase = Literal[_ds.PHASES]  # type: ignore[valid-type]

# Sorts an attempt without any timestamp last.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

router = APIRouter()


@router.get("/downloads")
def get_downloads(
    session: SessionDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    user: CurrentUserDep,
    viewer: ViewerDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None),
    q: str | None = Query(None),
    sort: DownloadSort = Query("recent"),
    phase: Annotated[list[DownloadPhase] | None, Query()] = None,
):
    """
    Ritorna i download raggruppati per track.
    ``phase`` (ripetibile) filtra per la fase dello stato AGGREGATO della
    traccia (vedi ``GET /config/download-states``), la stessa che conta
    ``GET /downloads/summary``; ``status`` resta il filtro sul singolo
    tentativo.
    Ogni elemento contiene la track completa, lo stato aggregato e la lista dei tentativi.

    Un admin vede la cronologia dell'intera istanza. Un utente non-admin vede
    solo i propri download: le righe di cui è il richiedente originale
    (``Download.requested_by``, colonna indicizzata) OPPURE a cui si è
    agganciato (``DownloadFollower``, vedi ``attach_to_open_download`` —
    ``visible_to_user_clause``).
    """
    is_admin = user.role == "admin"
    sync_types = sync_id_types(registry)
    # The phase filter, the count and every row's state from ONE snapshot: a
    # job committing in between (the sync indexing) never makes a row
    # disagree with the filter that selected it.
    read_snapshot(session)

    count_query = select(func.count(func.distinct(Download.track_id)))
    if status:
        count_query = count_query.where(Download.status == status)
    if not is_admin:
        count_query = count_query.where(visible_to_user_clause(user.id))

    last_activity = func.max(Download.started_at).label("last_activity")
    group_query = (
        select(Download.track_id, last_activity)
        .group_by(Download.track_id)
    )
    if status:
        group_query = group_query.where(Download.status == status)
    if not is_admin:
        group_query = group_query.where(visible_to_user_clause(user.id))

    if phase:
        ranks = track_status_ranks(None if is_admin else user.id, sync_types)
        wanted = [rank for p in phase for rank in ranks_of_phase(p)]
        in_phase = select(ranks.c.track_id).where(ranks.c.rank.in_(wanted))
        count_query = count_query.where(Download.track_id.in_(in_phase))
        group_query = group_query.where(Download.track_id.in_(in_phase))

    if q and q.strip():
        pattern = f"%{q.strip()}%"
        matching_ids = select(TrackRecord.internal_id).where(
            TrackRecord.title.ilike(pattern)
            | TrackRecord.artist.ilike(pattern)
            | TrackRecord.album.ilike(pattern)
        )
        count_query = count_query.where(Download.track_id.in_(matching_ids))
        group_query = group_query.where(Download.track_id.in_(matching_ids))

    if sort == "title":
        # The sort column is grouped too (one value per track): PostgreSQL asks for it.
        group_query = group_query.join(TrackRecord, Download.track_id == TrackRecord.internal_id).group_by(TrackRecord.title)
        order_clause = TrackRecord.title.asc()
    elif sort == "artist":
        group_query = group_query.join(TrackRecord, Download.track_id == TrackRecord.internal_id).group_by(TrackRecord.artist)
        order_clause = TrackRecord.artist.asc()
    else:
        order_clause = descending(last_activity)

    total = session.exec(count_query).one()

    offset = (page - 1) * limit
    track_ids_rows = session.exec(
        group_query.order_by(order_clause).offset(offset).limit(limit)
    ).all()
    track_ids = [row[0] for row in track_ids_rows]

    if not track_ids:
        return paginated_response([], total=total, page=page, limit=limit)

    downloads_query = select(Download).where(Download.track_id.in_(track_ids))
    if not is_admin:
        # Non solo i track_id: anche i singoli tentativi di ALTRI utenti sullo
        # stesso track vanno esclusi, altrimenti requested_by di un altro
        # utente trapelerebbe negli "attempts" di questa risposta. Un
        # follower vede l'attempt a cui si è agganciato, non gli altri
        # tentativi (anche falliti) di sconosciuti sulla stessa traccia.
        downloads_query = downloads_query.where(visible_to_user_clause(user.id))
    downloads = session.exec(downloads_query).all()

    tracks = session.exec(
        select(TrackRecord).where(TrackRecord.internal_id.in_(track_ids))
    ).all()
    track_map = {t.internal_id: t for t in tracks}

    id_records = session.exec(
        select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id.in_(track_ids))
    ).all()
    ids_by_track: dict[str, list[TrackIdentifierRecord]] = {}
    for r in id_records:
        ids_by_track.setdefault(r.track_id, []).append(r)

    owned = in_library(session, track_ids, sync_types)
    dl_by_track: dict[str, list[Download]] = {}
    for dl in downloads:
        dl_by_track.setdefault(dl.track_id, []).append(dl)

    requester_ids = {dl.requested_by for dl in downloads if dl.requested_by}
    username_by_id: dict[str, str] = {}
    if requester_ids:
        for u in session.exec(select(User).where(User.id.in_(requester_ids))).all():
            username_by_id[u.id] = u.username

    track_dicts: list[dict] = []
    items = []
    for tid in track_ids:
        record = track_map.get(tid)
        attempts = dl_by_track.get(tid, [])
        # Fallback a _EPOCH (non ""): un tentativo senza timestamp
        # produceva una chiave stringa, che confrontata con le datetime degli
        # altri tentativi sollevava TypeError → 500 su tutta la pagina Download.
        attempts.sort(key=lambda d: as_utc(d.started_at or d.completed_at) or _EPOCH, reverse=True)

        agg_status = aggregate_status(attempts, tid in owned)
        done_attempt = next((a for a in attempts if a.status == _ds.DOWNLOADED), None)
        requester_id = (done_attempt or attempts[0]).requested_by if attempts else None

        track_dict = public_track_dict(tid, record, ids_by_track.get(tid, []))
        track_dicts.append(track_dict)

        items.append({
            "track_id": tid,
            "status": agg_status,
            "provider_used": done_attempt.provider_used if done_attempt else None,
            "file_path": done_attempt.file_path if done_attempt else None,
            "requested_by": username_by_id.get(requester_id) if requester_id else None,
            "track": track_dict,
            "attempts": [
                {
                    "download_id": a.id,
                    "status": a.status,
                    "provider_used": a.provider_used,
                    "file_path": a.file_path,
                    **_attempt_error(a.error_message, show_detail=is_admin),
                    "retry_count": a.retry_count,
                    "started_at": utc_iso(a.started_at),
                    "completed_at": utc_iso(a.completed_at),
                }
                for a in attempts
            ],
        })

    enrich_dicts_with_stream_url(track_dicts, registry, auth_manager)
    annotate_tracks(track_dicts, session, viewer, registry)

    return paginated_response(items, total=total, page=page, limit=limit)


@router.get("/downloads/summary")
def get_downloads_summary(session: SessionDep, user: CurrentUserDep, registry: RegistryDep,
                          days: int = Query(7, ge=1, le=90)):
    """Contatori della pagina Download e della Home: tracce per stato del
    file e per fase (una traccia in libreria è ``done``, qualunque siano i
    suoi tentativi), e quante sono arrivate in libreria negli ultimi
    ``days`` giorni. Stessa visibilità di ``GET /downloads``."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    return status_summary(session, None if user.role == "admin" else user.id, since=since,
                          sync_id_types=sync_id_types(registry))


def _attempt_error(raw: str | None, *, show_detail: bool) -> dict:
    """L'errore di un tentativo: codice e messaggio leggibile; il testo
    grezzo (percorsi, eccezioni dell'infrastruttura) solo agli admin."""
    if not raw:
        return {"error_code": None, "error_message": None}
    return public_error(raw, "download_provider", show_detail=show_detail)


@router.post("/downloads/{track_id}/retry")
def retry_download(track_id: str, session: SessionDep, user: DownloaderDep, registry: RegistryDep, config: ConfigDep,
                   add_to_favorites: bool | None = Query(None), force: bool = Query(False)):
    """
    Ritenta il download di una track con gli identifier già salvati nel DB,
    dalla stessa porta di ``POST /download/track``: una traccia già in
    libreria non si riscarica (``already_in_library``, salvo ``force``, solo
    admin), una con una richiesta in corso vi si aggancia (``attached``).

    Accoda lavoro reale esattamente come le quattro route ``/download/*``,
    quindi segue la stessa regola: ``DownloaderDep``, e per un utente non
    admin il preferito all'arrivo (``add_to_favorites`` vale solo per un admin).

    Un retry non è una richiesta: rifà un download che esiste, e un download
    esiste solo perché una richiesta è stata approvata
    (``services/download_requests.py``). Per questo un utente soggetto alla
    policy di approvazione può ritentare solo una traccia di cui ha un
    download (suo, o a cui si è agganciato): per ogni altra, 403
    ``retry_not_approved``, mai un download che nessuno ha approvato.
    """
    user.check_force(force)
    track_record = session.get(TrackRecord, track_id)
    if not track_record:
        raise HTTPException(status_code=404, detail="Track not found")
    if not may_retry(session, user.user, track_id, config):
        raise HTTPException(status_code=403, detail={
            "error_code": "retry_not_approved",
            "error_message": "Only a download that was requested and approved can be retried.",
        })
    result = retry_track(session, track_record, user.requested_by, user.favorite_for(add_to_favorites), registry, force=force)
    return result.as_dict()


@router.post("/downloads/retry-failed")
def retry_failed_downloads(session: SessionDep, user: DownloaderDep, registry: RegistryDep,
                           add_to_favorites: bool | None = Query(None)):
    """
    Ritenta ogni traccia il cui download è fallito, fra quelle che chi chiama
    vede in ``GET /downloads``. Stessa regola del retry singolo. Idempotente:
    una traccia ritentata non è più fallita, quindi una seconda chiamata non
    accoda nulla. Ritorna gli id dei download accodati.
    """
    visible = select(Download.track_id)
    if not user.is_admin:
        visible = visible.where(visible_to_user_clause(user.user.id))
    # Lo stato di una traccia tiene conto di tutti i suoi tentativi, anche di
    # altri utenti: una traccia che qualcuno ha poi scaricato non è fallita.
    statuses: dict[str, list[str]] = {}
    for track_id, status in session.exec(
        select(Download.track_id, Download.status).where(Download.track_id.in_(visible))
    ).all():
        statuses.setdefault(track_id, []).append(status)
    owned = in_library(session, list(statuses), sync_id_types(registry))
    failed = [
        track_id for track_id, track_statuses in statuses.items()
        if _ds.file_state(track_statuses, track_id in owned) == _ds.FAILED
    ]
    if not failed:
        return {"download_ids": []}

    queued: list[str] = []
    favorite_for = user.favorite_for(add_to_favorites)
    for track_record in session.exec(select(TrackRecord).where(TrackRecord.internal_id.in_(failed))).all():
        result = retry_track(session, track_record, user.requested_by, favorite_for, registry)
        if result.status == QUEUED:
            queued.append(result.download_id)
    return {"download_ids": queued}
