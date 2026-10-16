"""Playlists (``playlists``, ``playlist_tracks``, ``playlist_syncs``): data
access only. The rules (who may edit, the cap, what a followed playlist is)
live in ``services/playlists.py``; the positions of a playlist are contiguous
from 0 and rewritten here on every change, always under the playlist's lock
(``db.locks.xact_lock``: two edits of one playlist never interleave)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from db.models import Playlist, PlaylistSync, PlaylistTrack, UserFavorite


def now() -> datetime:
    return datetime.now(timezone.utc)


def lock_name(playlist_id: str) -> str:
    return f"playlist:{playlist_id}"


def get(session: Session, playlist_id: str) -> Playlist | None:
    return session.get(Playlist, playlist_id)


def list_for_user(session: Session, user_id: str) -> list[Playlist]:
    return list(session.exec(
        select(Playlist).where(Playlist.user_id == user_id).order_by(Playlist.created_at, Playlist.id)
    ).all())


def all_followed(session: Session) -> list[Playlist]:
    return list(session.exec(
        select(Playlist).where(Playlist.source_provider.is_not(None)).order_by(Playlist.created_at, Playlist.id)
    ).all())


def find_followed(session: Session, user_id: str, provider: str, source_id: str) -> Playlist | None:
    return session.exec(select(Playlist).where(
        Playlist.user_id == user_id, Playlist.source_provider == provider, Playlist.source_id == source_id,
    )).first()


def touch(session: Session, playlist: Playlist) -> None:
    playlist.updated_at = now()
    session.add(playlist)


def track_rows(session: Session, playlist_id: str) -> list[PlaylistTrack]:
    return list(session.exec(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id).order_by(PlaylistTrack.position)
    ).all())


def track_counts(session: Session, playlist_ids: Iterable[str]) -> dict[str, int]:
    ids = list(playlist_ids)
    if not ids:
        return {}
    rows = session.exec(
        select(PlaylistTrack.playlist_id, func.count()).where(PlaylistTrack.playlist_id.in_(ids))
        .group_by(PlaylistTrack.playlist_id)
    ).all()
    return {playlist_id: count for playlist_id, count in rows}


def count(session: Session, playlist_id: str) -> int:
    return track_counts(session, [playlist_id]).get(playlist_id, 0)


def append_tracks(session: Session, playlist_id: str, track_ids: Sequence[str], *, download_requested=()) -> int:
    """Appends ``track_ids`` (the caller dropped the ones already in) after the
    last position; returns how many. ``download_requested``: the ones the
    playlist asked a download for."""
    if not track_ids:
        return 0
    last = session.exec(select(func.max(PlaylistTrack.position)).where(PlaylistTrack.playlist_id == playlist_id)).one()
    position = 0 if last is None else last + 1
    flagged = set(download_requested)
    for track_id in track_ids:
        session.add(PlaylistTrack(
            playlist_id=playlist_id, track_id=track_id, position=position, download_requested=track_id in flagged,
        ))
        position += 1
    return len(track_ids)


def remove_tracks(session: Session, playlist_id: str, track_ids: Iterable[str]) -> int:
    """Takes the tracks out and closes the gaps; returns how many were in."""
    wanted = set(track_ids)
    rows = track_rows(session, playlist_id)
    kept = [row for row in rows if row.track_id not in wanted]
    for row in rows:
        if row.track_id in wanted:
            session.delete(row)
    session.flush()
    _renumber(kept)
    return len(rows) - len(kept)


def reorder(session: Session, playlist_id: str, ordered_track_ids: Sequence[str]) -> None:
    """Puts the tracks in ``ordered_track_ids`` order (the caller checked it is
    exactly the playlist's tracks)."""
    by_track = {row.track_id: row for row in track_rows(session, playlist_id)}
    _renumber([by_track[track_id] for track_id in ordered_track_ids])


def replace_tracks(session: Session, playlist_id: str, ordered_track_ids: Sequence[str], *, download_requested=()) -> None:
    """Makes the playlist hold exactly ``ordered_track_ids``, in order: the
    rows of tracks that stay keep their flag and date, the others go, the new
    ones come with ``download_requested`` as given."""
    existing = {row.track_id: row for row in track_rows(session, playlist_id)}
    wanted = list(dict.fromkeys(ordered_track_ids))
    for track_id, row in existing.items():
        if track_id not in set(wanted):
            session.delete(row)
    session.flush()
    flagged = set(download_requested)
    rows: list[PlaylistTrack] = []
    for track_id in wanted:
        row = existing.get(track_id)
        if row is None:
            row = PlaylistTrack(playlist_id=playlist_id, track_id=track_id, download_requested=track_id in flagged)
            session.add(row)
        rows.append(row)
    _renumber(rows)


def _renumber(rows: Sequence[PlaylistTrack]) -> None:
    for position, row in enumerate(rows):
        row.position = position


def tracks_in_other_playlists(session: Session, track_ids: Iterable[str], excluding_playlist_id: str) -> set[str]:
    ids = list(track_ids)
    if not ids:
        return set()
    return set(session.exec(
        select(PlaylistTrack.track_id).where(
            PlaylistTrack.track_id.in_(ids), PlaylistTrack.playlist_id != excluding_playlist_id,
        )
    ).all())


def tracks_in_any_playlist(session: Session, track_ids: Iterable[str]) -> set[str]:
    ids = list(track_ids)
    if not ids:
        return set()
    return set(session.exec(select(PlaylistTrack.track_id).where(PlaylistTrack.track_id.in_(ids))).all())


def favorited_by_anyone(session: Session, track_ids: Iterable[str]) -> set[str]:
    ids = list(track_ids)
    if not ids:
        return set()
    return set(session.exec(select(UserFavorite.track_id).where(UserFavorite.track_id.in_(ids))).all())


def delete_playlist(session: Session, playlist: Playlist) -> None:
    """The playlist and its tracks. Its copies on the sync providers
    (``playlist_syncs``) stay until the routine has deleted them there."""
    session.exec(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id))
    session.delete(playlist)


def delete_tracks_of_user(session: Session, user_id: str) -> None:
    """The tracks of every playlist of the user (an account's deletion)."""
    ids = select(Playlist.id).where(Playlist.user_id == user_id)
    session.exec(delete(PlaylistTrack).where(PlaylistTrack.playlist_id.in_(ids)))


# ------------------------------------------------------------------
# Copies on the sync providers
# ------------------------------------------------------------------


def sync_rows(session: Session, user_id: str, sync_provider: str) -> dict[str, PlaylistSync]:
    """The user's copies on ``sync_provider``, by playlist id."""
    return {row.playlist_id: row for row in session.exec(select(PlaylistSync).where(
        PlaylistSync.user_id == user_id, PlaylistSync.sync_provider == sync_provider,
    )).all()}


def syncs_of_playlist(session: Session, playlist_id: str) -> list[PlaylistSync]:
    return list(session.exec(select(PlaylistSync).where(PlaylistSync.playlist_id == playlist_id)).all())


def orphan_syncs(session: Session) -> list[PlaylistSync]:
    """The copies whose playlist was deleted: to delete on the provider."""
    existing = select(Playlist.id)
    return list(session.exec(select(PlaylistSync).where(PlaylistSync.playlist_id.not_in(existing))).all())


def users_with_playlists(session: Session) -> list[str]:
    return list(session.exec(select(Playlist.user_id).distinct().order_by(Playlist.user_id)).all())
