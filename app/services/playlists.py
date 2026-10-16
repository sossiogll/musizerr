"""
The playlists of a user, kept by Musizerr (``docs/playlists.md``).

- A playlist is PRIVATE: its owner only (anyone else gets "not found").
- ``own`` playlists are edited by their owner (add, remove, reorder, rename).
- ``followed`` playlists mirror a playlist of a metadata provider: read-only,
  re-read when the provider's version marker changes (``refresh``), duplicated
  into an own playlist to edit them (``duplicate``). Following asks to download
  the tracks through the approval policy (``services/download_requests.py``)
  and does NOT make them favorites.
- A per-playlist cap (``playlist_max_tracks``, an admin setting) applies to
  every kind.
- The copies on the sync providers are pushed by a routine
  (``services/playlist_sync.py``); a playlist deleted here is deleted there by
  the same routine.

Every edit of one playlist runs under its lock (``db.locks.xact_lock``): two
requests never interleave their positions or both slip under the cap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlmodel import Session, select

from core import download_states as ds
from core import urls
from musizerr_sdk.models import Playlist as SourcePlaylist
from core.serialization import serialize_track
from core.utc import utc_iso
from db import playlist_store as store
from db.locks import xact_lock
from db.models import (
    PLAYLIST_FOLLOWED,
    PLAYLIST_OWN,
    Playlist,
    PlaylistTrack,
    TrackRecord,
    User,
)
from db.track_store import find_or_create_track, track_data_dict
from services import public_urls
from services.config_manager import ConfigManager
from services.download_requests import playlist_spec, request_download
from services.stored_tracks import public_track_dicts
from services.track_identity import with_sync_aliases
from services.track_state import annotate_tracks, file_state_of
from workers.celery_app import celery

logger = logging.getLogger(__name__)

# The most tracks one add request may carry (a page of a list, an album).
MAX_ADD_BATCH = 200

# Stable codes of what can go wrong (``PlaylistError.code``).
NOT_FOUND = "playlist_not_found"
READ_ONLY = "playlist_read_only"
FULL = "playlist_full"
INVALID_ORDER = "playlist_invalid_order"
TRACK_NOT_FOUND = "track_not_found"
SOURCE_NOT_FOUND = "playlist_source_not_found"
NOT_FOLLOWED = "playlist_not_followed"

# What a download requested with a follow/refresh came to (``download.status``).
DOWNLOAD_QUEUED = "queued"
DOWNLOAD_PENDING = "pending_approval"
DOWNLOAD_NONE = "none"        # nothing to ask for: the tracks are in the library or on their way
DOWNLOAD_ERROR = "error"      # the request itself failed: the playlist stays, the page can ask again

TASK_DELETE_TRACKS = "workers.maintenance.delete_playlist_tracks"


class PlaylistError(Exception):
    """A rule of the playlists said no. ``code`` is stable (the API answers
    with it), ``status`` the HTTP status that fits, ``extra`` goes in the body."""

    def __init__(self, code: str, message: str, status: int = 409, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error_code": self.code, "error_message": str(self), **self.extra}


def ensure_editable(playlist: Playlist) -> None:
    """Only an own playlist has its tracks edited."""
    if playlist.kind != PLAYLIST_OWN:
        raise _read_only()


def ensure_followed(playlist: Playlist) -> None:
    if playlist.kind != PLAYLIST_FOLLOWED:
        raise PlaylistError(NOT_FOLLOWED, "Only a followed playlist has a source to look at.", status=422)


def _full(cap: int, count: int, adding: int) -> PlaylistError:
    return PlaylistError(
        FULL, f"A playlist holds at most {cap} tracks.", max_tracks=cap, track_count=count, adding=adding,
    )


def _read_only() -> PlaylistError:
    return PlaylistError(READ_ONLY, "A followed playlist cannot be edited: duplicate it to get one of your own.")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def owned(session: Session, user_id: str, playlist_id: str) -> Playlist:
    """The playlist of ``user_id``; anyone else's is as good as missing."""
    playlist = store.get(session, playlist_id)
    if playlist is None or playlist.user_id != user_id:
        raise PlaylistError(NOT_FOUND, "Playlist not found.", status=404)
    return playlist


def _first_track_covers(session: Session, playlist_ids: list[str]) -> dict[str, str | None]:
    if not playlist_ids:
        return {}
    rows = session.exec(
        select(PlaylistTrack.playlist_id, TrackRecord)
        .join(TrackRecord, PlaylistTrack.track_id == TrackRecord.internal_id)
        .where(PlaylistTrack.playlist_id.in_(playlist_ids), PlaylistTrack.position == 0)
    ).all()
    covers: dict[str, str | None] = {}
    for playlist_id, record in rows:
        data = track_data_dict(record) or {}
        covers[playlist_id] = public_urls.publish(data.get("cover_url"))
    return covers


def public_playlists(session: Session, playlists: list[Playlist]) -> list[dict]:
    """The playlists as the API lists them (no tracks): the track count, the
    cover (the source's own, or the first track's) and where each one is
    mirrored on a sync provider."""
    ids = [playlist.id for playlist in playlists]
    counts = store.track_counts(session, ids)
    covers = _first_track_covers(session, [p.id for p in playlists if not p.cover_url])
    syncs: dict[str, list] = {playlist_id: [] for playlist_id in ids}
    for playlist_id in ids:
        syncs[playlist_id] = store.syncs_of_playlist(session, playlist_id)
    return [
        _public(playlist, counts.get(playlist.id, 0), covers.get(playlist.id), syncs[playlist.id])
        for playlist in playlists
    ]


def _public(playlist: Playlist, track_count: int, first_cover: str | None, syncs) -> dict:
    return {
        "id": playlist.id,
        "name": playlist.name,
        "description": playlist.description,
        "kind": playlist.kind,
        "source_provider": playlist.source_provider,
        "source_id": playlist.source_id,
        "cover_url": public_urls.publish(playlist.cover_url) or first_cover,
        "track_count": track_count,
        "auto_download": playlist.auto_download,
        "tracks_unavailable": (
            {"code": playlist.tracks_unavailable_code, "message": playlist.tracks_unavailable_message or ""}
            if playlist.tracks_unavailable_code else None
        ),
        "refresh_error_code": playlist.refresh_error_code,
        "checked_at": utc_iso(playlist.checked_at) if playlist.checked_at else None,
        "created_at": utc_iso(playlist.created_at),
        "updated_at": utc_iso(playlist.updated_at),
        "synced_to": [{"sync_provider": row.sync_provider, "synced_at": utc_iso(row.synced_at)} for row in syncs],
    }


def public_playlist(session: Session, playlist: Playlist) -> dict:
    return public_playlists(session, [playlist])[0]


def playlist_detail(session: Session, playlist: Playlist, viewer, registry) -> dict:
    """The playlist with its tracks, in order, annotated like any track list
    (library, file state, favorite, playback)."""
    rows = store.track_rows(session, playlist.id)
    records = {
        record.internal_id: record
        for record in session.exec(select(TrackRecord).where(TrackRecord.internal_id.in_([r.track_id for r in rows])))
    } if rows else {}
    ordered = [(row, records[row.track_id]) for row in rows if row.track_id in records]
    tracks = public_track_dicts(session, [record for _, record in ordered])
    for (row, _), track in zip(ordered, tracks):
        track["added_at"] = utc_iso(row.added_at)
    annotate_tracks(tracks, session, viewer, registry)
    return {**public_playlist(session, playlist), "tracks": tracks}


# ---------------------------------------------------------------------------
# Own playlists
# ---------------------------------------------------------------------------


def create(session: Session, user_id: str, name: str, description: str = "") -> Playlist:
    playlist = Playlist(user_id=user_id, name=name.strip(), description=description.strip(), kind=PLAYLIST_OWN)
    session.add(playlist)
    session.commit()
    return playlist


def update(
    session: Session, playlist: Playlist, *, name: str | None = None, description: str | None = None,
    auto_download: bool | None = None,
) -> Playlist:
    """Renames or re-describes an own playlist; a followed one only chooses
    whether the tracks its source gains are downloaded."""
    followed = playlist.kind == PLAYLIST_FOLLOWED
    if (name is not None or description is not None) and followed:
        raise _read_only()
    if auto_download is not None and not followed:
        raise PlaylistError(NOT_FOLLOWED, "Only a followed playlist downloads what its source gains.", status=422)
    if name is not None:
        playlist.name = name.strip()
    if description is not None:
        playlist.description = description.strip()
    if auto_download is not None:
        playlist.auto_download = auto_download
    store.touch(session, playlist)
    session.commit()
    return playlist


@dataclass(frozen=True)
class TrackRef:
    """A track to put in a playlist: one the host already knows
    (``track_id``), or one a provider names (``provider`` + ``provider_id``)."""

    track_id: str | None = None
    provider: str | None = None
    provider_id: str | None = None


def resolve_refs(session: Session, refs: Iterable[TrackRef], registry, fetch_track) -> list[str]:
    """The stored track id of each ref, in order, duplicates dropped.
    ``fetch_track(provider, provider_id)`` reads a track from its provider
    (the route wraps the call: a provider failure is not an error of the host).
    A ``track_id`` the host does not know is a 404 ``track_not_found``."""
    ids: list[str] = []
    for ref in refs:
        if ref.track_id:
            if session.get(TrackRecord, ref.track_id) is None:
                raise PlaylistError(TRACK_NOT_FOUND, "Track not found.", status=404, track_id=ref.track_id)
            track_id = ref.track_id
        else:
            track = fetch_track(ref.provider, ref.provider_id)
            if track is None:
                raise PlaylistError(TRACK_NOT_FOUND, "Track not found.", status=404, provider=ref.provider,
                                    provider_id=ref.provider_id)
            track_id = find_or_create_track(with_sync_aliases(track, registry), session).internal_id
        if track_id not in ids:
            ids.append(track_id)
    return ids


def add_tracks(session: Session, playlist: Playlist, track_ids: list[str], config: ConfigManager) -> dict:
    """Appends the tracks not already in the playlist. All or nothing: a batch
    that would take it over the cap is refused (409 ``playlist_full``)."""
    ensure_editable(playlist)
    playlist_id = playlist.id
    xact_lock(session, store.lock_name(playlist_id))
    held = {row.track_id for row in store.track_rows(session, playlist_id)}
    new = [track_id for track_id in dict.fromkeys(track_ids) if track_id not in held]
    cap = config.playlist_max_tracks
    if len(held) + len(new) > cap:
        session.rollback()
        raise _full(cap, len(held), len(new))
    store.append_tracks(session, playlist_id, new)
    playlist = store.get(session, playlist_id)
    store.touch(session, playlist)
    session.commit()
    return {"added": len(new), "skipped_duplicates": len(track_ids) - len(new)}


def remove_track(session: Session, playlist: Playlist, track_id: str) -> bool:
    """Takes one track out (False when it was not there)."""
    ensure_editable(playlist)
    playlist_id = playlist.id
    xact_lock(session, store.lock_name(playlist_id))
    removed = store.remove_tracks(session, playlist_id, [track_id]) > 0
    playlist = store.get(session, playlist_id)
    store.touch(session, playlist)
    session.commit()
    return removed


def reorder(session: Session, playlist: Playlist, ordered_track_ids: list[str]) -> None:
    """Puts the tracks in the order given, which must be exactly the playlist's
    tracks (422 ``playlist_invalid_order`` otherwise: a client that read an old
    list never loses or invents a track)."""
    ensure_editable(playlist)
    playlist_id = playlist.id
    xact_lock(session, store.lock_name(playlist_id))
    held = [row.track_id for row in store.track_rows(session, playlist_id)]
    if len(ordered_track_ids) != len(held) or set(ordered_track_ids) != set(held):
        session.rollback()
        raise PlaylistError(INVALID_ORDER, "The order must list exactly the tracks of the playlist, once each.",
                            status=422)
    store.reorder(session, playlist_id, ordered_track_ids)
    playlist = store.get(session, playlist_id)
    store.touch(session, playlist)
    session.commit()


def duplicate(session: Session, playlist: Playlist, name: str | None = None) -> Playlist:
    """An own, editable copy of any playlist of the user (a followed one
    included): the tracks as they are now."""
    copy_name = (name or "").strip() or f"{playlist.name} (copy)"
    source_id = playlist.id
    description = playlist.description
    copy = Playlist(user_id=playlist.user_id, name=copy_name, description=description, kind=PLAYLIST_OWN)
    session.add(copy)
    session.flush()
    store.append_tracks(session, copy.id, [row.track_id for row in store.track_rows(session, source_id)])
    session.commit()
    return copy


# ---------------------------------------------------------------------------
# Following a playlist of a metadata provider
# ---------------------------------------------------------------------------


@dataclass
class FollowResult:
    playlist: Playlist
    created: bool
    download: dict[str, Any] = field(default_factory=lambda: {"status": DOWNLOAD_NONE})


def _unique_tracks(source: SourcePlaylist) -> list:
    """The tracks of a source playlist, a repeated one kept once (same
    provider id)."""
    seen: set[tuple[str, str]] = set()
    tracks = []
    for track in source.tracks:
        key = (track.provider, track.provider_id)
        if key not in seen:
            seen.add(key)
            tracks.append(track)
    return tracks


def _resolve_tracks(session: Session, tracks, registry) -> list[tuple[Any, str]]:
    """Each source track with its stored track, in order, one per song."""
    pairs: list[tuple[Any, str]] = []
    seen: set[str] = set()
    for track in tracks:
        track_id = find_or_create_track(with_sync_aliases(track, registry), session).internal_id
        if track_id not in seen:
            seen.add(track_id)
            pairs.append((track, track_id))
    return pairs


def _needing_download(session: Session, pairs: list[tuple[Any, str]], registry) -> set[str]:
    """The stored ids, among the tracks, that are not in the library and have no
    download on its way (a failed one needs another)."""
    states = file_state_of([serialize_track(track) for track, _ in pairs], session, registry)
    return {track_id for (_, track_id), state in zip(pairs, states) if state is None or state == ds.FAILED}


def _ask_download(
    session: Session, user: User, source: SourcePlaylist, tracks, *, config, registry, auth_manager,
) -> dict[str, Any]:
    """Asks for the download of ``tracks`` of the source playlist, as the user
    and through the approval policy; they do NOT become favorites. The outcome
    is data (``download``): a request that fails does not undo the follow."""
    if not tracks:
        return {"status": DOWNLOAD_NONE}
    subset = SourcePlaylist(
        name=source.name, provider=source.provider, provider_id=source.provider_id, cover_url=source.cover_url,
        tracks=list(tracks),
    )
    try:
        outcome = request_download(
            session, user, playlist_spec(subset, only_missing=True), favorite_for=None, config=config,
            registry=registry, auth_manager=auth_manager,
        )
    except Exception:  # noqa: BLE001 — the playlist is already kept; the download can be asked again
        logger.exception("Download of playlist %s/%s could not be requested", source.provider, source.provider_id)
        session.rollback()
        return {"status": DOWNLOAD_ERROR}
    if outcome.pending:
        return {"status": DOWNLOAD_PENDING, **{k: v for k, v in outcome.pending_body().items() if k != "status"}}
    batch = outcome.execution.batch if outcome.execution else None
    queued = batch.queued if batch else 0
    return {"status": DOWNLOAD_QUEUED if queued else DOWNLOAD_NONE, "queued": queued,
            "skipped": batch.skipped if batch else 0}


def follow(
    session: Session, user: User, source: SourcePlaylist, *, config: ConfigManager, registry, auth_manager,
) -> FollowResult:
    """Follows the playlist ``source`` (read from its provider by the caller):
    keeps its tracks, asks to download them and watches it for changes.
    Following twice is the same follow. A playlist whose tracks the provider
    does not give is followed all the same, with the reason
    (``tracks_unavailable``) and nothing to download. A playlist longer than the
    cap is refused (409 ``playlist_full``)."""
    existing = store.find_followed(session, user.id, source.provider, source.provider_id)
    if existing is not None:
        return FollowResult(existing, created=False)

    unavailable = source.tracks_unavailable
    tracks = [] if unavailable else _unique_tracks(source)
    cap = config.playlist_max_tracks
    if len(tracks) > cap:
        raise _full(cap, 0, len(tracks))
    pairs = _resolve_tracks(session, tracks, registry)
    track_ids = [track_id for _, track_id in pairs]
    needing = _needing_download(session, pairs, registry)

    user_id = user.id
    xact_lock(session, f"playlist-follow:{user_id}:{source.provider}:{source.provider_id}")
    existing = store.find_followed(session, user_id, source.provider, source.provider_id)
    if existing is not None:
        session.rollback()
        return FollowResult(existing, created=False)
    playlist = Playlist(
        user_id=user_id, name=source.name, description=source.description, kind=PLAYLIST_FOLLOWED,
        source_provider=source.provider, source_id=source.provider_id, snapshot_id=source.snapshot_id,
        cover_url=urls.to_host_ref(source.cover_url), auto_download=True,
        tracks_unavailable_code=unavailable.code if unavailable else None,
        tracks_unavailable_message=unavailable.message if unavailable else None,
        checked_at=store.now(),
    )
    session.add(playlist)
    session.flush()
    store.append_tracks(session, playlist.id, track_ids, download_requested=needing)
    session.commit()

    download = _ask_download(
        session, user, source, [track for track, _ in pairs], config=config, registry=registry,
        auth_manager=auth_manager,
    )
    return FollowResult(store.get(session, playlist.id), created=True, download=download)


@dataclass
class RefreshResult:
    """How looking at a followed playlist's source ended."""

    changed: bool = False
    new_tracks: int = 0
    removed_tracks: int = 0
    error_code: str | None = None
    download: dict[str, Any] = field(default_factory=lambda: {"status": DOWNLOAD_NONE})


def refresh(
    session: Session, user: User, playlist: Playlist, meta, auth_session, *, config: ConfigManager, registry,
    auth_manager, force: bool = False,
) -> RefreshResult:
    """Looks at the source of a followed playlist. The provider's snapshot
    (``MetadataProvider.get_playlist_snapshot``) decides, cheaply, whether
    anything changed; only then the tracks are read again. The tracks the
    source gained are added (and downloaded when ``auto_download``), the ones
    it lost are dropped (their files stay); the order is the source's. A source
    that gives no tracks leaves the playlist as it is. ``force`` reads the
    tracks whatever the snapshot says. Provider failures are the caller's
    (they raise)."""
    ensure_followed(playlist)
    playlist_id, source_id = playlist.id, playlist.source_id
    marker = meta.get_playlist_snapshot(source_id, auth_session)
    if (not force and marker is not None and marker == playlist.snapshot_id
            and playlist.refresh_error_code is None and playlist.tracks_unavailable_code is None):
        playlist.checked_at = store.now()
        session.add(playlist)
        session.commit()
        return RefreshResult()

    source = meta.get_playlist(source_id, auth_session)
    playlist = store.get(session, playlist_id)
    if source is None:
        playlist.refresh_error_code = SOURCE_NOT_FOUND
        playlist.checked_at = store.now()
        session.add(playlist)
        session.commit()
        return RefreshResult(error_code=SOURCE_NOT_FOUND)

    result = RefreshResult()
    unavailable = source.tracks_unavailable
    playlist.checked_at = store.now()
    playlist.refresh_error_code = None
    playlist.tracks_unavailable_code = unavailable.code if unavailable else None
    playlist.tracks_unavailable_message = unavailable.message if unavailable else None
    if unavailable:
        session.add(playlist)
        session.commit()
        return result

    tracks = _unique_tracks(source)
    cap = config.playlist_max_tracks
    if len(tracks) > cap:
        playlist.refresh_error_code = FULL
        session.add(playlist)
        session.commit()
        return RefreshResult(error_code=FULL)
    pairs = _resolve_tracks(session, tracks, registry)
    track_ids = [track_id for _, track_id in pairs]
    xact_lock(session, store.lock_name(playlist_id))
    playlist = store.get(session, playlist_id)
    held = [row.track_id for row in store.track_rows(session, playlist_id)]
    gained = {track_id for track_id in track_ids if track_id not in set(held)}
    lost = [track_id for track_id in held if track_id not in set(track_ids)]
    gained_pairs = [(track, track_id) for track, track_id in pairs if track_id in gained]
    needing = _needing_download(session, gained_pairs, registry)

    result.changed = bool(
        gained or lost or track_ids != held or source.name != playlist.name
        or source.description != playlist.description
    )
    result.new_tracks, result.removed_tracks = len(gained), len(lost)
    store.replace_tracks(session, playlist_id, track_ids, download_requested=needing)
    playlist.name = source.name or playlist.name
    playlist.description = source.description
    playlist.cover_url = urls.to_host_ref(source.cover_url) or playlist.cover_url
    playlist.snapshot_id = marker if marker is not None else source.snapshot_id
    if result.changed:
        store.touch(session, playlist)
    session.add(playlist)
    auto_download = playlist.auto_download
    session.commit()

    if gained_pairs and auto_download:
        result.download = _ask_download(
            session, user, source, [track for track, _ in gained_pairs], config=config, registry=registry,
            auth_manager=auth_manager,
        )
    return result


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def downloads_to_delete(session: Session, playlist: Playlist) -> list[str]:
    """The tracks that are the playlist's to take with it: the ones it asked a
    download for (not in the library before), that no other playlist holds and
    no user has as a favorite."""
    rows = [row for row in store.track_rows(session, playlist.id) if row.download_requested]
    ids = [row.track_id for row in rows]
    kept = store.tracks_in_other_playlists(session, ids, playlist.id) | store.favorited_by_anyone(session, ids)
    return [track_id for track_id in ids if track_id not in kept]


def delete(session: Session, playlist: Playlist, *, delete_tracks: bool = False) -> dict[str, Any]:
    """Deletes (or, for a followed one, unfollows) the playlist. By default
    only the playlist goes: the tracks stay in the library. With
    ``delete_tracks`` the files that were downloaded for this playlist, and
    nothing else holds, are deleted too (a task, ``downloads_to_delete``).
    Its copies on the sync providers go with the next run of the routine."""
    owner_id = playlist.user_id
    candidates = downloads_to_delete(session, playlist) if delete_tracks else []
    xact_lock(session, store.lock_name(playlist.id))
    store.delete_playlist(session, playlist)
    session.commit()
    if candidates:
        celery.send_task(TASK_DELETE_TRACKS, kwargs={"user_id": owner_id, "track_ids": candidates})
    return {"deleted": True, "tracks_scheduled": len(candidates)}
