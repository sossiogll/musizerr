"""
Followed playlists, watched (routine ``refresh_followed_playlists``).

Every followed playlist is looked at with its owner's accounts: the provider's
version marker (``MetadataProvider.get_playlist_snapshot``) says whether it
changed, and only then are its tracks read again (``services.playlists.refresh``).
A source that cannot be read is recorded on the playlist
(``refresh_error_code``) and in the run's result — the run is an ``error``,
never ``ok`` with the failure hidden inside; the next run tries again. A playlist
whose provider is gone or whose owner has no account on it is skipped, not a
failure. When something changed, the push to the sync providers is started
(``sync_playlists``).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session

from core.auth_manager import AuthError
from core.routines import SYNC_PLAYLISTS
from db import playlist_store as store
from db.models import User
from services import playlists, routines
from services.auth import auth_manager_for
from services.credentials import holds_accounts, user_store
from services.errors import public_error

logger = logging.getLogger(__name__)

_MAX_ERROR_DETAIL = 300


def refresh_all(session: Session, registry, config, *, progress=None) -> dict[str, Any]:
    """Looks at every followed playlist; one that fails does not stop the
    others. ``progress`` (``services.routines.RunProgress``) hears where it is."""
    followed = store.all_followed(session)
    result: dict[str, Any] = {
        "playlists": len(followed), "checked": 0, "changed": 0, "new_tracks": 0, "removed_tracks": 0,
        "download_requests": 0, "skipped": 0, "failed": [],
    }
    for index, playlist in enumerate(followed, start=1):
        if progress is not None:
            progress("refreshing", provider=playlist.source_provider, processed=index - 1, total=len(followed))
        playlist_id = playlist.id
        outcome = _refresh_one(session, registry, config, playlist)
        if outcome is None:
            result["skipped"] += 1
            continue
        result["checked"] += 1
        if isinstance(outcome, dict):
            result["failed"].append({"playlist_id": playlist_id, **outcome})
            continue
        result["changed"] += int(outcome.changed)
        result["new_tracks"] += outcome.new_tracks
        result["removed_tracks"] += outcome.removed_tracks
        result["download_requests"] += int(outcome.download.get("status") in (
            playlists.DOWNLOAD_QUEUED, playlists.DOWNLOAD_PENDING))
        if outcome.error_code:
            result["failed"].append({"playlist_id": playlist_id, "error_code": outcome.error_code,
                                     "error_message": None, "error_detail": None})
    if result["changed"]:
        routines.start_routine(SYNC_PLAYLISTS)
    if result["failed"]:
        first = result["failed"][0]
        result.update(status="error", error_code=first["error_code"], error_message=first.get("error_message"),
                      error_detail=first.get("error_detail"))
    else:
        result["status"] = "ok"
    return result


def _refresh_one(session: Session, registry, config, playlist):
    """``RefreshResult``; None when the playlist cannot be looked at (its
    provider is not installed, its owner has no account on it); a dict with
    the error when the provider failed."""
    playlist_id, owner_id = playlist.id, playlist.user_id
    meta = registry.get_metadata_provider(playlist.source_provider)
    owner = session.get(User, owner_id)
    if meta is None or not holds_accounts(session, owner):
        return None
    auth_manager = auth_manager_for(user_store(session, owner_id))
    try:
        auth_session = auth_manager.authenticate(meta)
    except AuthError:
        return None
    try:
        return playlists.refresh(
            session, owner, playlist, meta, auth_session, config=config, registry=registry, auth_manager=auth_manager,
        )
    except Exception as exc:  # noqa: BLE001 — one playlist's failure never stops the others
        session.rollback()
        logger.warning("Refreshing playlist %s failed: %s", playlist_id, exc)
        body = public_error(exc, "metadata_provider", show_detail=True)
        body["error_detail"] = (body["error_detail"] or "")[:_MAX_ERROR_DETAIL]
        stored = store.get(session, playlist_id)
        if stored is not None:
            stored.refresh_error_code = body["error_code"]
            stored.checked_at = store.now()
            session.add(stored)
            session.commit()
        return body
