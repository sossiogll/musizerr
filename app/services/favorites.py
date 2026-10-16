"""
Favorites: a user's liked tracks, always tracks in the library (favorites ⊆
library, ``db.library_store``).

- The heart (``add`` / ``remove``) acts at once and propagates the like to
  the sync provider through the like reconciliation (``PendingLike`` in the
  sync phase, ``workers.maintenance.reconcile_like``). Removing a favorite
  never deletes the file: the library is shared by every user.
- A download with favorite registers an intent (``register_intent``); when the
  track lands, ``apply_intents`` makes it a favorite of everyone who asked.
- The like matching (``match_likes``) links the likes imported from other
  providers (``UserSavedTrack``) to tracks already in the library with the
  core matching, and makes them favorites once, downloading nothing.
- A favorite the user takes off stays off (``RemovedFavorite``): the removal
  is remembered by what names the song (``core.matching.song_keys``), not by
  a record id, and honoured by every automatic path (the like matching, the
  likes download) however the like comes back — a re-import onto another
  record, a wipe of the imported likes, a like that is still on the origin
  provider. Only the user's own heart or download clears it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.matching import base_title_key, match_track, song_keys
from db import favorite_store
from db.library_store import in_library, library_clause
from musizerr_sdk.identifiers import Identifier
from db.models import TrackIdentifierRecord, TrackRecord, UserSavedTrack
from db.pending_like_store import delete_pending_like, ensure_pending_like, favorite_intents, mark_queued
from db.track_store import identifiers_by_track, track_from_record
from services.config_manager import get_config
from services.settings import personal_value
from services.track_identity import sync_aliases, sync_provider_names
from services.track_state import sync_id_types
from workers.celery_app import celery

logger = logging.getLogger(__name__)

HEART, DOWNLOAD, MATCH = "heart", "download", "match"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dispatch(task_name: str, **kwargs) -> None:
    """Queues a worker task by its registered name (the only way services
    reach the workers)."""
    celery.send_task(task_name, kwargs=kwargs)


class NotInLibrary(Exception):
    """The track is not in the library: it cannot be a favorite."""


def is_in_library(session: Session, track_id: str, registry) -> bool:
    return track_id in in_library(session, [track_id], sync_id_types(registry))


def add(session: Session, user_id: str, track_id: str, registry, source: str = HEART) -> bool:
    """Makes a library track a favorite (idempotent: False if it already
    was) and propagates the like to the sync provider."""
    if not is_in_library(session, track_id, registry):
        raise NotInLibrary(track_id)
    return _add_in_library(session, user_id, track_id, source)


def _add_in_library(session: Session, user_id: str, track_id: str, source: str, *, also_named_by=()) -> bool:
    """The favorite itself. A source of the user's own (the heart, a download
    of theirs) forgets that they once took the song off; an automatic one
    (``MATCH``) adds nothing for a song they took off, whichever record it
    comes on: ``also_named_by`` are the other records (the like) whose names
    count too."""
    if source == MATCH:
        if is_removed(session, user_id, track_id, *also_named_by):
            return False
    else:
        favorite_store.clear_removal(session, user_id, _names_of(session, [track_id]))
    created = favorite_store.add_favorite(session, user_id, track_id, source)
    sync_name = get_config().sync_provider
    if sync_name:
        marker_id = ensure_pending_like(session, user_id, track_id, sync_name)
        # A run only when no attempt is queued or scheduled already (it
        # would stop at the claim anyway: wasted work).
        if mark_queued(session, marker_id, _now()):
            dispatch("workers.maintenance.reconcile_like", pending_like_id=marker_id)
    else:
        delete_pending_like(session, user_id, track_id)
    return created


def _names_of(session: Session, track_ids) -> set[str]:
    """``core.matching.song_keys`` of each stored track."""
    ids = [track_id for track_id in dict.fromkeys(track_ids) if track_id]
    identifiers = identifiers_by_track(session, ids)
    names: set[str] = set()
    for record in session.exec(select(TrackRecord).where(TrackRecord.internal_id.in_(ids))).all() if ids else []:
        names |= song_keys(track_from_record(record, identifiers.get(record.internal_id, ())))
    return names


def is_removed(session: Session, user_id: str, *track_ids: str) -> bool:
    """Whether the user took off a favorite that is the song of any of these
    stored tracks (``RemovedFavorite``)."""
    return favorite_store.is_removed(session, user_id, _names_of(session, track_ids))


def remove(session: Session, user_id: str, track_id: str) -> bool:
    """Removes the favorite (idempotent: False if it was not one) and any
    intent still open for it, remembers that the user took the song off (it
    stays off: ``RemovedFavorite``) and takes the like back on the sync
    provider and, if the user chose it (``unlike_on_origin_providers``), on
    the providers the like was imported from. The file stays in the library."""
    names = _names_of(session, [track_id])
    removed = favorite_store.remove_favorite(session, user_id, track_id)
    delete_pending_like(session, user_id, track_id)
    if not removed:
        return False
    favorite_store.record_removal(session, user_id, names)
    # Only if THIS user chose it (a personal setting); the task checks it again
    # when it runs and acts only with this user's own accounts.
    if personal_value(session, user_id, "unlike_on_origin_providers"):
        dispatch("workers.maintenance.unlike_on_origin_providers", user_id=user_id, track_id=track_id)
    sync_name = get_config().sync_provider
    if sync_name:
        dispatch("workers.maintenance.propagate_unlike", user_id=user_id, track_id=track_id, sync_provider=sync_name)
    return True


def add_many(session: Session, user_id: str, track_ids: list[str], registry) -> tuple[list[str], list[str]]:
    """Favorites the tracks of ``track_ids`` that are in the library; returns
    (added now, skipped: not in the library). Already favorite ones are
    neither."""
    wanted = list(dict.fromkeys(track_ids))
    present = in_library(session, wanted, sync_id_types(registry))
    added = [track_id for track_id in wanted if track_id in present and _add_in_library(session, user_id, track_id, HEART)]
    return added, [track_id for track_id in wanted if track_id not in present]


def register_intent(session: Session, user_id: str, track_id: str, registry) -> None:
    """A download with favorite: the track becomes a favorite of ``user_id``
    when it lands (at once, if it already is in the library). The user asked
    for it: a removal they made earlier no longer stands."""
    favorite_store.clear_removal(session, user_id, _names_of(session, [track_id]))
    if is_in_library(session, track_id, registry):
        _add_in_library(session, user_id, track_id, DOWNLOAD)
        return
    ensure_pending_like(session, user_id, track_id)


def apply_intents(session: Session, track_id: str) -> int:
    """The track landed: every "favorite on arrival" intent on it becomes a
    favorite. Returns how many."""
    intents = favorite_intents(session, track_id)
    for intent in intents:
        _add_in_library(session, intent.user_id, track_id, DOWNLOAD)
    return len(intents)


# ------------------------------------------------------------------
# Like matching
# ------------------------------------------------------------------


def match_likes(session: Session, registry, user_id: str | None = None) -> dict[str, int]:
    """Links every like imported from another provider that has no library
    track yet to one in the library, with ``core.matching.match_track``
    (candidates narrowed by base title or by a shared identifier, never a
    bespoke comparison), and makes that track a favorite. A like is matched
    once: a favorite removed later is not added back by the next run.
    Downloads nothing.

    Every identifier counts, not only ISRC/MBID: the like and the library
    record may be the same item of the same provider, and a like imported
    from the sync provider carries as provider id the value the library
    record holds as sync id (one identity space,
    ``services.track_identity``). The identifiers are read from the
    identifiers table, which knows more than the stored track data."""
    query = select(UserSavedTrack).where(UserSavedTrack.matched_at.is_(None))
    if user_id is not None:
        query = query.where(UserSavedTrack.user_id == user_id)
    likes = session.exec(query).all()
    if not likes:
        return {"checked": 0, "matched": 0}

    library_ids = set(session.exec(library_clause(sync_id_types(registry))).scalars().all())
    records = [r for r in session.exec(select(TrackRecord)).all() if r.internal_id in library_ids]
    by_id = {record.internal_id: record for record in records}
    by_title: dict[str, list[TrackRecord]] = {}
    for record in records:
        by_title.setdefault(base_title_key(record.title), []).append(record)

    liked_ids = {like.track_id for like in likes}
    sync_names = sync_provider_names(registry)
    ids_of: dict[str, list[Identifier]] = {}
    for row in session.exec(select(TrackIdentifierRecord)).all():
        if row.track_id in library_ids or row.track_id in liked_ids:
            ids_of.setdefault(row.track_id, []).append(Identifier.from_dict({"type": row.id_type, "value": row.value}))
    for track_id, identifiers in ids_of.items():
        identifiers.extend(sync_aliases(identifiers, sync_names))
    by_identifier: dict[tuple[str, str], list[str]] = {}
    for track_id in library_ids:
        for identifier in ids_of.get(track_id, []):
            by_identifier.setdefault((identifier.storage_type(), identifier.value), []).append(track_id)

    def candidates(liked: TrackRecord) -> list[TrackRecord]:
        found = {c.internal_id: c for c in by_title.get(base_title_key(liked.title), [])}
        for identifier in ids_of.get(liked.internal_id, []):
            for track_id in by_identifier.get((identifier.storage_type(), identifier.value), []):
                found.setdefault(track_id, by_id[track_id])
        return list(found.values())

    tracks: dict[str, object] = {}

    def track_of(record: TrackRecord):
        if record.internal_id not in tracks:
            track = track_from_record(record)
            for identifier in ids_of.get(record.internal_id, []):
                track.add_id(identifier)
            tracks[record.internal_id] = track
        return tracks[record.internal_id]

    liked_records = {
        r.internal_id: r for r in session.exec(select(TrackRecord).where(TrackRecord.internal_id.in_(liked_ids))).all()
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    matched = 0
    for like in likes:
        liked = liked_records.get(like.track_id)
        if liked is None:
            continue
        if like.track_id in library_ids:
            target = like.track_id
        else:
            # An exact match (a shared identifier) before a strong one; the
            # same pick in every run.
            source = track_of(liked)
            results = ((match_track(source, track_of(c)), c.internal_id) for c in candidates(liked))
            matches = sorted((result.confidence != "exact", track_id) for result, track_id in results if result.matched)
            target = matches[0][1] if matches else None
        if target is None:
            continue
        like.library_track_id = target
        like.matched_at = now
        session.add(like)
        session.commit()
        # Linked to the library either way (it is not "liked elsewhere" any
        # more); a favorite only if the user did not take the song off.
        _add_in_library(session, like.user_id, target, MATCH, also_named_by=[like.track_id])
        matched += 1
    logger.info("Like matching: %d of %d likes linked to the library", matched, len(likes))
    return {"checked": len(likes), "matched": matched}
