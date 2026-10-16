"""
The library, as the database knows it: which tracks have a file that is ours,
wherever it is stored. One definition, used by the track field
(``services.track_state``), every coverage count (``services.library_coverage``,
album/playlist/Home) and the favorites (``services.favorites``).

A track is IN THE LIBRARY when any of these holds:

- one of its downloads landed (``Download.status == downloaded``), unless
  the file it landed as is known missing;
- the storage holds a file of it (``LibraryEntry``: downloaded or found by
  the library scan), unless the entry is marked missing;
- a sync provider indexed it (a sync id in ``track_identifiers``: written by
  the like reconciler, the stream enrichment or the duplicate scan).

An entry is marked missing (``missing_since``) by the library file
reconciliation (``services/library_reconcile.py``) when the storage no longer
holds its file; a download whose ``file_path`` is such an entry's path no
longer counts either. A download that landed without a path (delegated to
an external system) still counts: there is no file to check.

Otherwise its file state is the aggregate of its download attempts (queued,
downloading, failed...), or nothing when it was never requested.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import or_, union
from sqlmodel import Session, select

from core import download_states as ds
from db.expressions import descending
from db.models import Download, LibraryEntry, TrackIdentifierRecord


def _missing_paths():
    """``SELECT file_path`` of the entries whose file the storage lost."""
    return select(LibraryEntry.file_path).where(LibraryEntry.missing_since.is_not(None))


def _landed():
    """A download that landed on a file still there (or on no file at all)."""
    return (
        Download.status == ds.DOWNLOADED,
        or_(Download.file_path.is_(None), Download.file_path.not_in(_missing_paths())),
    )


def _present():
    """An entry whose file the storage still holds."""
    return (LibraryEntry.track_id.is_not(None), LibraryEntry.missing_since.is_(None))


def library_clause(sync_id_types: Iterable[str]):
    """``SELECT track_id`` of every track in the library (a union, usable in
    ``IN (...)``)."""
    queries = [
        select(Download.track_id).where(*_landed()),
        select(LibraryEntry.track_id).where(*_present()),
    ]
    types = list(sync_id_types)
    if types:
        queries.append(select(TrackIdentifierRecord.track_id).where(TrackIdentifierRecord.id_type.in_(types)))
    return union(*queries)


def in_library(session: Session, track_ids: Iterable[str], sync_id_types: Iterable[str]) -> set[str]:
    """The ids, among ``track_ids``, of the tracks in the library."""
    ids = list({track_id for track_id in track_ids if track_id})
    if not ids:
        return set()
    found: set[str] = set()
    found.update(session.exec(select(Download.track_id).where(Download.track_id.in_(ids), *_landed())).all())
    found.update(session.exec(select(LibraryEntry.track_id).where(LibraryEntry.track_id.in_(ids), *_present())).all())
    types = list(sync_id_types)
    if types:
        found.update(session.exec(
            select(TrackIdentifierRecord.track_id).where(
                TrackIdentifierRecord.track_id.in_(ids), TrackIdentifierRecord.id_type.in_(types),
            )
        ).all())
    return found


def stored_file_paths(session: Session, track_id: str) -> list[str]:
    """The files of the track on the storage, best first: the downloads that
    landed (newest first), then the entries of the library scan. Files known
    missing are left out."""
    paths: list[str] = []
    for path in session.exec(
        select(Download.file_path)
        .where(Download.track_id == track_id, Download.file_path.is_not(None), *_landed())
        .order_by(descending(Download.completed_at))
    ).all():
        if path not in paths:
            paths.append(path)
    for path in session.exec(
        select(LibraryEntry.file_path)
        .where(LibraryEntry.track_id == track_id, *_present())
        .order_by(LibraryEntry.created_at.desc())
    ).all():
        if path not in paths:
            paths.append(path)
    return paths


def file_states(session: Session, track_ids: Iterable[str], sync_id_types: Iterable[str]) -> dict[str, str]:
    """The file state of each track that has one, in the download-state
    vocabulary: ``downloaded`` when it is in the library (any source), else
    the most advanced of its download attempts. Tracks never requested and
    not in the library are absent."""
    ids = list({track_id for track_id in track_ids if track_id})
    if not ids:
        return {}
    attempts: dict[str, list[str]] = {}
    for track_id, status in session.exec(
        select(Download.track_id, Download.status).where(Download.track_id.in_(ids))
    ).all():
        attempts.setdefault(track_id, []).append(status)
    owned = in_library(session, ids, sync_id_types)
    return _states(attempts, owned)


def _states(attempts: dict[str, list[str]], owned: set[str]) -> dict[str, str]:
    return {
        track_id: ds.file_state(attempts.get(track_id, ()), track_id in owned)
        for track_id in set(attempts) | owned
    }


def all_file_states(session: Session, sync_id_types: Iterable[str]) -> dict[str, str]:
    """``file_states`` of every track that has one, without an ``IN`` list
    (a whole library can be larger than SQLite's parameter limit)."""
    attempts: dict[str, list[str]] = {}
    for track_id, status in session.exec(select(Download.track_id, Download.status)).all():
        attempts.setdefault(track_id, []).append(status)
    owned = set(session.exec(library_clause(sync_id_types)).scalars().all())
    return _states(attempts, owned)
