"""
Deleting the files a playlist downloaded, when its owner asked for it along
with the playlist (``services.playlists.delete``: ``delete_tracks``).

The playlist chose the candidates when it was deleted (the tracks it asked a
download for); this runs later, in a task, and checks again, track by track,
that deleting is still right: no playlist holds the track, no user has it as a
favorite, every file of it was downloaded by Musizeer for this user (or for
nobody in particular: authentication off) — a file the library scan found, or
one somebody else asked for, is never touched. A failure on one track leaves
that track as it is and does not stop the others. The files are deleted
through the storage provider; the sync provider is told to rescan and the
library index drops what it no longer lists.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from core.routines import SYNC_LIBRARY_INDEX
from db import playlist_store as store
from db.models import Download, DownloadFollower, LibraryEntry
from services import routines
from services.storage import resolve_storage_provider

logger = logging.getLogger(__name__)

SOURCE_DOWNLOAD = "download"


def delete_downloads(
    session: Session, registry, config, auth_manager, user_id: str, track_ids: list[str],
) -> dict[str, Any]:
    """Deletes the files of ``track_ids`` that are still the playlist's to
    take. Returns the counts: ``deleted`` tracks, ``kept`` (a playlist or a
    favorite holds it, or it is not ours to delete), ``failed``."""
    report: dict[str, Any] = {"deleted": 0, "kept": 0, "failed": 0}
    storage, _ = resolve_storage_provider(registry, config.storage_provider)
    if storage is None:
        report["failed"] = len(track_ids)
        report["error_code"] = "no_storage_provider"
        return report
    storage_session = auth_manager.authenticate(storage)

    held = store.tracks_in_any_playlist(session, track_ids) | store.favorited_by_anyone(session, track_ids)
    for track_id in dict.fromkeys(track_ids):
        if track_id in held:
            report["kept"] += 1
            continue
        downloads = list(session.exec(select(Download).where(Download.track_id == track_id)).all())
        entries = list(session.exec(select(LibraryEntry).where(LibraryEntry.track_id == track_id)).all())
        if (any(d.requested_by not in (None, user_id) for d in downloads)
                or any(e.source != SOURCE_DOWNLOAD for e in entries)
                or not any(d.file_path for d in downloads) and not entries):
            report["kept"] += 1
            continue
        paths = list(dict.fromkeys([d.file_path for d in downloads if d.file_path] + [e.file_path for e in entries]))
        try:
            for path in paths:
                storage.delete(path, storage_session)
        except Exception as exc:  # noqa: BLE001 — this track stays as it is, the others go on
            logger.warning("Deleting the files of track %s failed: %s", track_id, exc)
            report["failed"] += 1
            continue
        download_ids = [d.id for d in downloads]
        if download_ids:
            session.exec(delete(DownloadFollower).where(DownloadFollower.download_id.in_(download_ids)))
            session.exec(delete(Download).where(Download.id.in_(download_ids)))
        for entry in entries:
            session.delete(entry)
        session.commit()
        report["deleted"] += 1

    if report["deleted"]:
        _rescan(registry, auth_manager)
        routines.start_routine(SYNC_LIBRARY_INDEX)
    return report


def _rescan(registry, auth_manager) -> None:
    """Best effort: the files are already gone."""
    for sync in registry.all_sync():
        try:
            sync.trigger_scan(auth_manager.authenticate(sync))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rescan on %s failed: %s", sync.name, exc)
