from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func, select as sa_select
from sqlmodel import func, select

from core.utc import utc_iso
from db import favorite_store, user_service
from db.library_store import library_clause
from db import library_job_store
from db.expressions import ascending, descending, json_number
from db.library_job_store import KIND_DOWNLOAD_SYNC, KIND_LIBRARY_SYNC, KINDS
from db.models import TrackRecord, UserFavorite, UserSavedTrack, UserSavedTrackProvider
from services import favorites, library_jobs
from services.enrichment import enrich_dicts_with_stream_url
from services.stored_tracks import public_track_dicts
from services.track_state import annotate_tracks, sync_id_types
from routes.deps import AuthManagerDep, CurrentUserDep, RegistryDep, SessionDep, ViewerDep
from routes._pagination import paginated_response

router = APIRouter()


LibrarySort = Literal[
    "added_desc", "added_asc",
    "synced_desc", "synced_asc",
    "title", "artist",
    "year_desc", "year_asc",
]

_earliest_added_at = (
    sa_select(sa_func.coalesce(
        func.min(UserSavedTrackProvider.added_at),
        func.min(UserSavedTrackProvider.synced_at),
    ))
    .where(UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
    .correlate(UserSavedTrack)
    .scalar_subquery()
)

_latest_synced_at = (
    sa_select(func.max(UserSavedTrackProvider.synced_at))
    .where(UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
    .correlate(UserSavedTrack)
    .scalar_subquery()
)

FavoriteSort = Literal["added_desc", "added_asc", "title", "artist", "year_desc", "year_asc"]

_YEAR = json_number(TrackRecord.track_data, "year")

_FAVORITE_SORT_MAP: dict[str, list] = {
    "added_desc": [UserFavorite.created_at.desc()],
    "added_asc": [UserFavorite.created_at.asc()],
    "title": [TrackRecord.title.asc()],
    "artist": [TrackRecord.artist.asc()],
    "year_desc": [descending(_YEAR)],
    "year_asc": [ascending(_YEAR)],
}

# title/artist sort on TrackRecord's real columns now that saved tracks join
# onto the canonical tracks table — only year is read from the JSON of
# track_data, since TrackRecord doesn't have a dedicated year column. The
# nullable ones (no year, no provider row) sort with NULL as the smallest
# value, written out (db.expressions).
_LIBRARY_SORT_MAP: dict[str, list] = {
    "added_desc": [descending(_earliest_added_at)],
    "added_asc": [ascending(_earliest_added_at)],
    "synced_desc": [descending(_latest_synced_at)],
    "synced_asc": [ascending(_latest_synced_at)],
    "title": [TrackRecord.title.asc()],
    "artist": [TrackRecord.artist.asc()],
    "year_desc": [descending(_YEAR)],
    "year_asc": [ascending(_YEAR)],
}


class FavoriteBatchBody(BaseModel):
    track_ids: list[str] = Field(..., max_length=500)


def _not_in_library(track_id: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "error_code": "not_in_library",
        "error_message": "Only a track in the library can be a favorite.",
        "track_id": track_id,
    })


@router.get("/favorites")
def get_favorites(
    session: SessionDep,
    user: CurrentUserDep,
    viewer: ViewerDep,
    registry: RegistryDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    q: str | None = Query(None),
    sort: FavoriteSort = Query("added_desc"),
):
    """The current user's favorites: tracks of the library they liked."""
    base = (
        select(UserFavorite, TrackRecord)
        .join(TrackRecord, UserFavorite.track_id == TrackRecord.internal_id)
        .where(UserFavorite.user_id == user.id)
    )
    count_q = (
        select(func.count())
        .select_from(UserFavorite)
        .join(TrackRecord, UserFavorite.track_id == TrackRecord.internal_id)
        .where(UserFavorite.user_id == user.id)
    )
    text_filter = _text_filter(q)
    if text_filter is not None:
        base = base.where(text_filter)
        count_q = count_q.where(text_filter)

    total = session.exec(count_q).one()
    rows = session.exec(
        base.order_by(*_FAVORITE_SORT_MAP[sort]).offset((page - 1) * limit).limit(limit)
    ).all()
    items = public_track_dicts(session, [record for _, record in rows])
    for (favorite, _), track in zip(rows, items):
        track["favorited_at"] = utc_iso(favorite.created_at)
    annotate_tracks(items, session, viewer, registry)
    return paginated_response(items, total=total, page=page, limit=limit)


@router.put("/favorites/{track_id}")
def add_favorite(track_id: str, session: SessionDep, user: CurrentUserDep, registry: RegistryDep):
    """The heart: makes a track of the library a favorite. Idempotent; 409
    ``not_in_library`` for a track that is not in the library."""
    if session.get(TrackRecord, track_id) is None:
        raise HTTPException(status_code=404, detail="Track not found")
    try:
        created = favorites.add(session, user.id, track_id, registry)
    except favorites.NotInLibrary:
        raise _not_in_library(track_id)
    return {"track_id": track_id, "favorite": True, "changed": created}


@router.delete("/favorites/{track_id}")
def remove_favorite(track_id: str, session: SessionDep, user: CurrentUserDep):
    """The heart, off: the track leaves the favorites, its file stays in the
    library. Idempotent."""
    removed = favorites.remove(session, user.id, track_id)
    return {"track_id": track_id, "favorite": False, "changed": removed}


@router.post("/favorites/batch")
def add_favorites(body: FavoriteBatchBody, session: SessionDep, user: CurrentUserDep, registry: RegistryDep):
    """Favorites several tracks at once (an album's or a playlist's "add the
    ones in library"): only the tracks in the library are added, the others
    are returned as ``skipped``."""
    added, skipped = favorites.add_many(session, user.id, body.track_ids, registry)
    return {"added": added, "skipped": skipped}


@router.get("/favorites/elsewhere")
def get_liked_elsewhere(
    session: SessionDep,
    user: CurrentUserDep,
    viewer: ViewerDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    provider: str | None = Query(None),
    q: str | None = Query(None),
    sort: LibrarySort = Query("added_desc"),
):
    """Tracks the user liked on other providers (imported by the library
    sync) that are not in the library: no library track matched them yet.
    They can be downloaded (for a user, download = favorite on arrival)."""
    library_ids = library_clause(sync_id_types(registry))
    conditions = [
        UserSavedTrack.user_id == user.id,
        UserSavedTrack.library_track_id.is_(None),
        UserSavedTrack.track_id.not_in(library_ids),
    ]
    base = (
        select(UserSavedTrack, TrackRecord)
        .join(TrackRecord, UserSavedTrack.track_id == TrackRecord.internal_id)
        .where(*conditions)
    )
    count_q = (
        select(func.count())
        .select_from(UserSavedTrack)
        .join(TrackRecord, UserSavedTrack.track_id == TrackRecord.internal_id)
        .where(*conditions)
    )

    if provider:
        provider_subq = select(UserSavedTrackProvider.saved_track_id).where(
            UserSavedTrackProvider.provider == provider
        )
        base = base.where(UserSavedTrack.id.in_(provider_subq))
        count_q = count_q.where(UserSavedTrack.id.in_(provider_subq))

    text_filter = _text_filter(q)
    if text_filter is not None:
        base = base.where(text_filter)
        count_q = count_q.where(text_filter)

    total = session.exec(count_q).one()
    offset = (page - 1) * limit
    rows = session.exec(
        base.order_by(*_LIBRARY_SORT_MAP[sort]).offset(offset).limit(limit)
    ).all()

    saved_ids = [saved.id for saved, _ in rows]
    prov_rows = session.exec(
        select(UserSavedTrackProvider).where(
            UserSavedTrackProvider.saved_track_id.in_(saved_ids)
        )
    ).all() if saved_ids else []

    prov_map: dict[str, dict] = {}
    for pr in prov_rows:
        prov_map.setdefault(pr.saved_track_id, {})[pr.provider] = {
            "added_at": utc_iso(pr.added_at),
        }

    items = public_track_dicts(session, [record for _, record in rows])
    for (saved, _), track in zip(rows, items):
        track["saved_at"] = utc_iso(saved.saved_at)
        track["provider_saved_at"] = prov_map.get(saved.id, {})

    enrich_dicts_with_stream_url(items, registry, auth_manager)
    annotate_tracks(items, session, viewer, registry)

    return paginated_response(items, total=total, page=page, limit=limit)


# After /favorites/elsewhere: a path parameter would swallow it.
@router.get("/favorites/{track_id}")
def get_favorite(track_id: str, session: SessionDep, user: CurrentUserDep, registry: RegistryDep):
    """Whether one track is in the library and among the user's favorites:
    what a row asks once its download lands (the favorite on arrival is
    applied by the server when the file arrives)."""
    if session.get(TrackRecord, track_id) is None:
        raise HTTPException(status_code=404, detail="Track not found")
    return {
        "track_id": track_id,
        "favorite": favorite_store.find_favorite(session, user.id, track_id) is not None,
        "in_library": favorites.is_in_library(session, track_id, registry),
    }


def _text_filter(q: str | None):
    if not q or not q.strip():
        return None
    pattern = f"%{q.strip()}%"
    return (
        TrackRecord.title.ilike(pattern)
        | TrackRecord.artist.ilike(pattern)
        | TrackRecord.album.ilike(pattern)
    )


@router.delete("/library/saved-tracks")
def delete_saved_tracks(session: SessionDep, user: CurrentUserDep):
    """Wipe the likes imported from other providers and the finished library
    syncs. Favorites stay (they are the user's own choice in Musizerr)."""
    user_service.delete_saved_tracks(session, user.id)
    library_job_store.delete_finished(session, user.id, KIND_LIBRARY_SYNC)
    session.commit()
    return {"ok": True}


def _start_job(session, user, kind: str, response: Response, params: dict | None = None) -> dict:
    """Starts the user's job of ``kind``, or returns the one already queued or
    running: 202 with ``created: true`` for a new run, 200 with the existing
    one. 503 ``queue_unavailable`` when the task cannot be enqueued."""
    try:
        run, created = library_jobs.start(session, user.id, kind, params)
    except library_jobs.QueueUnavailable:
        raise HTTPException(status_code=503, headers={"Retry-After": "30"}, detail={
            "error_code": library_jobs.QUEUE_UNAVAILABLE,
            "error_message": library_jobs.MESSAGES[library_jobs.QUEUE_UNAVAILABLE],
        })
    response.status_code = 202 if created else 200
    return {**library_jobs.public_run(run, show_detail=user.role == "admin"), "created": created}


@router.post("/library/sync")
def trigger_sync(session: SessionDep, user: CurrentUserDep, response: Response):
    """Imports the user's likes from their providers (idempotent: while a
    sync is queued or running, that run is returned)."""
    return _start_job(session, user, KIND_LIBRARY_SYNC, response)


@router.get("/library/sync/status")
def get_sync_status(session: SessionDep, user: CurrentUserDep):
    """The user's latest library sync, with its progress while it runs."""
    return library_jobs.latest_public_run(session, user.id, KIND_LIBRARY_SYNC, show_detail=user.role == "admin")


@router.post("/library/download-sync")
def trigger_download_sync(
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
    time_range: str = Query("all", pattern=r"^(all|1m|3m)$"),
):
    """Queues the downloads of the user's likes that are not in the library
    (idempotent like ``POST /library/sync``)."""
    return _start_job(session, user, KIND_DOWNLOAD_SYNC, response, {"time_range": time_range})


@router.get("/library/download-sync/status")
def get_download_sync_status(session: SessionDep, user: CurrentUserDep):
    """The user's latest "download missing", with its progress while it runs."""
    return library_jobs.latest_public_run(session, user.id, KIND_DOWNLOAD_SYNC, show_detail=user.role == "admin")


@router.get("/library/jobs")
def get_library_jobs(session: SessionDep, user: CurrentUserDep):
    """The user's latest run of every library job, in one read (the app
    shell's indicator and the library page)."""
    show_detail = user.role == "admin"
    return {kind: library_jobs.latest_public_run(session, user.id, kind, show_detail=show_detail) for kind in KINDS}
