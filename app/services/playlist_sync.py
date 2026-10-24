"""
The playlists of the users, mirrored on the sync providers (routine
``sync_playlists``).

A sync provider that keeps playlists (``SyncProvider.supports_playlists``) gets
each user's playlists on THAT USER'S OWN account (``own_store``: never a shared
one; a user without an account there is skipped), where a server shows them
natively. Only the tracks the provider holds have an id there (the library
index gave them one): a track not in the library yet is left out and comes in
a later run, once it landed and was indexed. The routine follows
``sync_library_index`` for that reason.

What was pushed is remembered (``playlist_syncs``: the id on the provider and a
hash of the name and the ids sent), so a playlist that did not change costs no
call. A playlist the user deleted leaves its row behind; the run deletes the
copy on the provider and then the row. A copy the user deleted on the provider
(``RemotePlaylistGone``) is created again. One provider or one user failing
does not stop the others: the run reports it and the next one tries again.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlmodel import Session, select

from core.identifiers import sync_id_type
from musizeer_sdk.errors import RemotePlaylistGone
from db import playlist_store as store
from db.models import Playlist, PlaylistSync, TrackIdentifierRecord
from services.auth import auth_manager_for
from services.credentials import has_own_account, own_store
from services.errors import public_error

logger = logging.getLogger(__name__)

_MAX_ERROR_DETAIL = 300


@dataclass
class SyncReport:
    sync_provider: str
    users: int = 0
    skipped_no_account: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    tracks_not_indexed: int = 0   # tracks left out: the provider holds no copy (yet)
    failed: int = 0
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def content_hash(name: str, sync_ids: list[str]) -> str:
    """What identifies the content of a copy: its name and the provider's ids
    of its tracks, in order."""
    return hashlib.sha256(json.dumps([name, sync_ids]).encode()).hexdigest()


def sync_ids_of(session: Session, sync_name: str, track_ids: list[str]) -> dict[str, str]:
    """The id each track has on the sync provider (one per track: the smallest
    when a track has several files)."""
    if not track_ids:
        return {}
    found: dict[str, str] = {}
    rows = session.exec(
        select(TrackIdentifierRecord.track_id, TrackIdentifierRecord.value).where(
            TrackIdentifierRecord.id_type == sync_id_type(sync_name), TrackIdentifierRecord.track_id.in_(track_ids),
        ).order_by(TrackIdentifierRecord.value)
    ).all()
    for track_id, value in rows:
        found.setdefault(track_id, value)
    return found


def push_all(session: Session, registry, *, progress=None) -> list[dict]:
    """``push_provider`` for every sync provider that keeps playlists."""
    reports = []
    for sync in registry.all_sync():
        if not sync.supports_playlists():
            continue
        if progress is not None:
            progress("pushing", provider=sync.name, force=True)
        reports.append(push_provider(session, sync).as_dict())
    return reports


def push_provider(session: Session, sync) -> SyncReport:
    report = SyncReport(sync_provider=sync.name)
    owners = sorted(set(store.users_with_playlists(session)) | {row.user_id for row in session.exec(
        select(PlaylistSync).where(PlaylistSync.sync_provider == sync.name)).all()})
    for user_id in owners:
        credentials = own_store(session, user_id)
        if not has_own_account(credentials, sync):
            report.skipped_no_account += 1
            continue
        report.users += 1
        try:
            auth_session = auth_manager_for(credentials).authenticate(sync)
            _push_user(session, sync, auth_session, user_id, report)
        except Exception as exc:  # noqa: BLE001 — one user's failure never stops the others
            _fail(session, report, exc)
    return report


def _fail(session: Session, report: SyncReport, exc: BaseException) -> None:
    session.rollback()
    logger.warning("Pushing playlists to %s failed: %s", report.sync_provider, exc)
    body = public_error(exc, "sync_provider", show_detail=True)
    body["error_detail"] = (body["error_detail"] or "")[:_MAX_ERROR_DETAIL]
    report.failed += 1
    report.errors.append(body)


def _push_user(session: Session, sync, auth_session, user_id: str, report: SyncReport) -> None:
    copies = store.sync_rows(session, user_id, sync.name)
    live = store.list_for_user(session, user_id)
    live_ids = {playlist.id for playlist in live}

    # Copies of playlists that are gone: delete them on the provider, then forget them.
    for playlist_id, row in list(copies.items()):
        if playlist_id in live_ids:
            continue
        try:
            sync.delete_playlist(row.remote_id, auth_session)
        except RemotePlaylistGone:
            pass
        session.delete(row)
        session.commit()
        report.deleted += 1

    for playlist in live:
        _push_playlist(session, sync, auth_session, playlist, copies.get(playlist.id), report)


def _push_playlist(
    session: Session, sync, auth_session, playlist: Playlist, row: PlaylistSync | None, report: SyncReport,
) -> None:
    playlist_id, user_id, name = playlist.id, playlist.user_id, playlist.name
    track_ids = [track.track_id for track in store.track_rows(session, playlist_id)]
    known = sync_ids_of(session, sync.name, track_ids)
    sync_ids = [known[track_id] for track_id in track_ids if track_id in known]
    report.tracks_not_indexed += len(track_ids) - len(sync_ids)
    digest = content_hash(name, sync_ids)

    if row is not None and row.content_hash == digest:
        report.unchanged += 1
        return
    try:
        if row is not None:
            sync.update_playlist(row.remote_id, name, sync_ids, auth_session)
            row.content_hash, row.synced_at = digest, store.now()
            session.add(row)
            report.updated += 1
        else:
            _create(session, sync, auth_session, user_id, playlist_id, name, sync_ids, digest)
            report.created += 1
    except RemotePlaylistGone:
        # The user deleted the copy on the provider: it is created again.
        if row is not None:
            session.delete(row)
            session.flush()
        _create(session, sync, auth_session, user_id, playlist_id, name, sync_ids, digest)
        report.created += 1
    session.commit()


def _create(session: Session, sync, auth_session, user_id: str, playlist_id: str, name: str,
            sync_ids: list[str], digest: str) -> None:
    remote_id = sync.create_playlist(name, sync_ids, auth_session)
    session.add(PlaylistSync(
        playlist_id=playlist_id, sync_provider=sync.name, user_id=user_id, remote_id=remote_id, content_hash=digest,
        synced_at=store.now(),
    ))


def push_result(reports: list[dict]) -> dict[str, Any]:
    """The result of the ``sync_playlists`` routine: one report per sync
    provider, the totals, and ``status: "error"`` with the first failure's code
    when a provider failed (the run is then recorded as failed)."""
    totals = ("created", "updated", "unchanged", "deleted", "tracks_not_indexed", "failed")
    result: dict[str, Any] = {name: sum(int(report.get(name) or 0) for report in reports) for name in totals}
    result["providers"] = reports
    failed = [report for report in reports if report.get("errors")]
    if failed:
        first = failed[0]["errors"][0]
        result.update(status="error", error_code=first["error_code"], error_message=first.get("error_message"),
                      error_detail=first.get("error_detail"), failed_providers=[r["sync_provider"] for r in failed])
    else:
        result["status"] = "ok"
    return result
