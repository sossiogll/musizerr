"""
The approval policy of download requests, applied.

``request_download`` is THE entry point of every user-facing way to ask for a
download — a track, an album (its delegation to an external system included),
a playlist, an artist, the "download the ones liked elsewhere" library job.
It runs BEFORE anything is queued or delegated:

1. a request the policy does not govern (an admin's, or any request with
   authentication off) and one that needs nothing downloaded (everything is in
   the library or already on its way) goes straight to the existing gate
   (``services/downloads.py``), exactly as before, and leaves no record;
2. any other request is counted (``needed_tracks``: the tracks that NEED a
   download, never the ones in the library or with an open download the
   request would attach to), decided by ``core.download_policy.decide`` and
   recorded in ``download_requests`` — approved at once or ``pending``.
   Nothing is queued, delegated or marked as a favorite while it waits: the
   tracks, the album and the favorite-on-arrival choice are stored with the
   request and applied, through the same gate, when an admin approves.

What is NOT governed: retries of a download that already exists
(``routes/history.py``, ``retry_transient_failures``), whose approval was the
one that created it — ``may_retry`` makes sure a retry cannot be
used to start a download nobody approved; and the admins' own requests.

Deciding is one conditional UPDATE (``db.download_request_store.claim``): of
any number of concurrent approvals, denials and cancellations of one request
exactly one wins, and only the winner queues the downloads. The decision of a
new request and the quota it reads happen under a per-user lock
(``db.locks.xact_lock``) and the quota is reserved (``needed_tracks``) before
the downloads are queued, so two requests of one user never both fit under
the same room.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from core import download_policy as policy
from core import download_states as ds
from core import urls
from musizerr_sdk.errors import DownloadDelegated
from musizerr_sdk.models import Album, Track
from core.serialization import deserialize_album, deserialize_track, serialize_album, serialize_track
from core.utc import utc_iso
from db import download_request_store as store
from db.locks import xact_lock
from db.models import SYSTEM_USER_ID, Download, DownloadRequest, User
from db.download_store import visible_to_user_clause
from services import public_urls
from services.config_manager import ConfigManager
from services.downloads import BatchResult, download_album, enqueue_track, enqueue_tracks
from services.progress import publish_event
from services.request_policy import governed, policy_for
from services.track_state import file_state_of

logger = logging.getLogger(__name__)

# What the download routes answer (202) while a request waits.
PENDING_APPROVAL = "pending_approval"

EVENT_TYPE = "download_request"
# Events of a request (``event`` in the progress stream).
EVENT_PENDING = "pending"
EVENT_APPROVED = "approved"
EVENT_DENIED = "denied"
EVENT_CANCELLED = "cancelled"

# How a decision on a request ended (``DecisionResult.outcome``).
DECIDED = "decided"        # this call changed the request
UNCHANGED = "unchanged"    # it was already in the state asked for: nothing done
CONFLICT = "conflict"      # it was already decided otherwise
NOT_FOUND = "not_found"


def _now() -> datetime:
    """The clock of the policy: the decision time and the quota window (a
    test drives it)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# What a request asks for
# ---------------------------------------------------------------------------


@dataclass
class DownloadSpec:
    """What a request asks to download: the ``kind``, its tracks (an album's
    included: they are the album's tracks) and, for an album, the album itself
    (an external system may take the whole release). Everything but ``force``
    is stored with a request that has to wait."""

    kind: str
    tracks: list[Track]
    target_provider: str = ""
    target_id: str = ""
    title: str = ""
    subtitle: str = ""
    cover_url: str | None = None
    album: Album | None = None
    only_missing: bool = False
    # Download again a track already in the library: admin only, so it never
    # waits and is never stored.
    force: bool = False


def track_spec(track: Track, *, force: bool = False) -> DownloadSpec:
    return DownloadSpec(
        policy.KIND_TRACK, [track], track.provider, track.provider_id, track.title, track.artist, track.cover_url,
        force=force,
    )


def album_spec(album: Album, *, only_missing: bool = False) -> DownloadSpec:
    return DownloadSpec(
        policy.KIND_ALBUM, list(album.tracks), album.provider, album.provider_id, album.title, album.artist,
        album.cover_url, album=album, only_missing=only_missing,
    )


def playlist_spec(playlist, *, only_missing: bool = False) -> DownloadSpec:
    return DownloadSpec(
        policy.KIND_PLAYLIST, list(playlist.tracks), playlist.provider, playlist.provider_id, playlist.name, "",
        playlist.cover_url, only_missing=only_missing,
    )


def artist_spec(artist, tracks: list[Track]) -> DownloadSpec:
    return DownloadSpec(
        policy.KIND_ARTIST, list(tracks), artist.provider, artist.provider_id, artist.name, "", artist.cover_url,
    )


def liked_elsewhere_spec(tracks: list[Track]) -> DownloadSpec:
    """The likes of a user that are not in the library: no target, so a user
    has one such request waiting at a time (a second run while it waits finds
    it, see ``request_download``)."""
    return DownloadSpec(policy.KIND_LIKED_ELSEWHERE, list(tracks))


def _payload(spec: DownloadSpec) -> str:
    """What approval needs to run the request later, in stored form (host URLs
    as references: ``core.urls``)."""
    album = None
    if spec.album is not None:
        album = serialize_album(spec.album)
        album.pop("tracks")
        album["cover_url"] = urls.to_host_ref(album["cover_url"])
    return json.dumps({
        "album": album,
        "tracks": [urls.track_dict_for_storage(serialize_track(track)) for track in spec.tracks],
    })


def _spec_of(request: DownloadRequest) -> DownloadSpec:
    """The request, as it will run (the inverse of ``_payload``)."""
    data = json.loads(request.payload or "{}")
    tracks = [deserialize_track(track) for track in data.get("tracks", [])]
    album = deserialize_album({**data["album"], "tracks": data.get("tracks", [])}) if data.get("album") else None
    return DownloadSpec(
        request.kind, tracks, request.target_provider, request.target_id, request.title, request.subtitle,
        request.cover_url, album=album, only_missing=request.only_missing,
    )


# ---------------------------------------------------------------------------
# Running a request through the existing gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Execution:
    """What running a request did: the tracks that went through the gate
    (``batch``), or the album an external system took (``delegated``)."""

    batch: BatchResult | None = None
    delegated: DownloadDelegated | None = None

    def queued_tracks(self, needed: int) -> int:
        """The new downloads: counted by the gate; an album an external system
        took counts as the tracks it needed."""
        return needed if self.delegated is not None else (self.batch.queued if self.batch else 0)


def execute(
    session: Session, spec: DownloadSpec, *, user_id: str | None, favorite_for: str | None, registry, auth_manager,
    config: ConfigManager,
) -> Execution:
    """Runs ``spec`` through the gate of ``services/downloads.py``: the same
    code for a request approved at once, one an admin approves later and one
    the policy does not govern. ``favorite_for`` is the user whose favorite
    the tracks become when they arrive (the intent is registered by the gate,
    now, not before)."""
    if spec.kind == policy.KIND_TRACK:
        result = enqueue_track(
            spec.tracks[0], session, user_id=user_id, favorite_for=favorite_for, registry=registry, force=spec.force,
        )
        return Execution(batch=BatchResult([result], skipped=0))
    if spec.kind == policy.KIND_ALBUM and spec.album is not None:
        album = download_album(
            spec.album, session, registry, auth_manager, config, user_id=user_id, only_missing=spec.only_missing,
            favorite_for=favorite_for,
        )
        return Execution(batch=album.batch, delegated=album.delegated)
    return Execution(batch=enqueue_tracks(
        spec.tracks, session, registry, user_id=user_id, only_missing=spec.only_missing, favorite_for=favorite_for,
    ))


def needed_tracks(session: Session, tracks: list[Track], registry) -> int:
    """How many of ``tracks`` need a download: the ones not in the library and
    with no open download the request would attach to (a failed one needs it
    again). Read-only, the same file state as ``only_missing``."""
    states = file_state_of([serialize_track(track) for track in tracks], session, registry)
    return sum(1 for state in states if state is None or state == ds.FAILED)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What ``request_download`` did: ``execution`` when the request went
    through the gate (the policy approved it, did not govern it, or it needed
    nothing); ``request`` is its record when it was recorded; with no
    ``execution`` the request waits for an admin."""

    execution: Execution | None = None
    request: DownloadRequest | None = None

    @property
    def pending(self) -> bool:
        return self.execution is None

    def pending_body(self) -> dict[str, Any]:
        """The 202 answer of a request that waits."""
        assert self.request is not None
        return {
            "status": PENDING_APPROVAL,
            "request_id": self.request.id,
            "kind": self.request.kind,
            "needed_tracks": self.request.needed_tracks,
            "reason": self.request.reason,
        }


def request_download(
    session: Session, user: User, spec: DownloadSpec, *, favorite_for: str | None, config: ConfigManager,
    registry, auth_manager,
) -> Outcome:
    """Asks for the download of ``spec`` on behalf of ``user``, applying the
    approval policy (see the module docstring). ``favorite_for`` is the user
    the tracks become a favorite of when they arrive (``Downloader.favorite_for``)."""
    requester = None if user.id == SYSTEM_USER_ID else user.id
    run = dict(favorite_for=favorite_for, registry=registry, auth_manager=auth_manager, config=config)
    if not governed(user, config):
        return Outcome(execution=execute(session, spec, user_id=requester, **run))
    needed = needed_tracks(session, spec.tracks, registry)
    if needed == 0:
        return Outcome(execution=execute(session, spec, user_id=requester, **run))

    now = _now()
    # One decision at a time per user: the pending request of the same target,
    # the quota used and the record that reserves the room are read and written
    # under this lock (held until the commit).
    xact_lock(session, f"download-request-user:{user.id}")
    existing = store.find_pending(session, user.id, spec.kind, spec.target_provider, spec.target_id)
    if existing is not None:
        existing_id = existing.id
        session.rollback()
        return Outcome(request=store.get(session, existing_id))
    effective = policy_for(session, config, user.id)
    used = store.tracks_in_window(session, user.id, now - timedelta(days=effective.quota_days))
    decision = policy.decide(effective, spec.kind, needed, used)
    request = DownloadRequest(
        user_id=user.id, kind=spec.kind, target_provider=spec.target_provider, target_id=spec.target_id,
        title=spec.title, subtitle=spec.subtitle, cover_url=urls.to_host_ref(spec.cover_url), payload=_payload(spec),
        needed_tracks=needed, add_to_favorites=favorite_for is not None, only_missing=spec.only_missing,
        status=policy.STATUS_APPROVED if decision.approved else policy.STATUS_PENDING,
        auto=decision.approved, reason=decision.reason,
        decided_at=now if decision.approved else None, created_at=now,
    )
    session.add(request)
    request_id = request.id
    session.commit()

    if not decision.approved:
        request = store.get(session, request_id)
        _notify_admins(session, EVENT_PENDING, request)
        return Outcome(request=request)

    try:
        execution = execute(session, spec, user_id=user.id, **run)
    except Exception:
        # Nothing went through the gate: the request never took effect.
        session.rollback()
        store.delete(session, request_id)
        raise
    store.record_outcome(
        session, request_id, queued=execution.queued_tracks(needed),
        dispatched_to=execution.delegated.provider if execution.delegated else None,
    )
    return Outcome(execution=execution, request=store.get(session, request_id))


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionResult:
    request: DownloadRequest | None
    outcome: str


def _decide(
    session: Session, request_id: str, to_status: str, *, by: User, note: str | None = None,
    requester: str | None = None,
) -> DecisionResult:
    """Moves a pending request to ``to_status`` if, and only if, this call is
    the one that finds it pending (``store.claim``); otherwise says what the
    request is: already in that state (idempotent), decided otherwise, or not
    there (for a requester that does not own it: the same, never a leak)."""
    claimed = store.claim(
        session, request_id, to_status, decided_by=by.id, decided_at=_now(), note=note, requester=requester,
    )
    request = store.get(session, request_id)
    if request is None or (requester is not None and request.user_id != requester):
        return DecisionResult(None, NOT_FOUND)
    if claimed:
        return DecisionResult(request, DECIDED)
    return DecisionResult(request, UNCHANGED if request.status == to_status else CONFLICT)


def approve(
    session: Session, request_id: str, admin: User, *, note: str | None, config: ConfigManager, registry, auth_manager,
) -> DecisionResult:
    """An admin approves a request: it is queued through the gate exactly once
    (only the call that wins the claim queues it), with the favorite-on-arrival
    choice stored with the request. When running it fails before any track
    went in, the request goes back to pending and the error is raised."""
    result = _decide(session, request_id, policy.STATUS_APPROVED, by=admin, note=note)
    if result.outcome != DECIDED:
        return result
    request = result.request
    assert request is not None
    try:
        execution = execute(
            session, _spec_of(request), user_id=request.user_id,
            favorite_for=request.user_id if request.add_to_favorites else None,
            registry=registry, auth_manager=auth_manager, config=config,
        )
    except Exception:
        session.rollback()
        store.reopen(session, request.id, admin.id)
        raise
    store.record_outcome(
        session, request.id, queued=execution.queued_tracks(request.needed_tracks),
        dispatched_to=execution.delegated.provider if execution.delegated else None,
    )
    request = store.get(session, request.id)
    _notify_requester(EVENT_APPROVED, request)
    _notify_admins(session, EVENT_APPROVED, request)
    return DecisionResult(request, DECIDED)


def deny(session: Session, request_id: str, admin: User, *, reason: str | None) -> DecisionResult:
    """An admin denies a request: nothing was queued and no favorite intent was
    registered, so there is nothing to undo."""
    result = _decide(session, request_id, policy.STATUS_DENIED, by=admin, note=reason)
    if result.outcome == DECIDED:
        _notify_requester(EVENT_DENIED, result.request)
        _notify_admins(session, EVENT_DENIED, result.request)
    return result


def cancel(session: Session, request_id: str, user: User) -> DecisionResult:
    """A user cancels their own pending request."""
    result = _decide(session, request_id, policy.STATUS_CANCELLED, by=user, requester=user.id)
    if result.outcome == DECIDED:
        _notify_admins(session, EVENT_CANCELLED, result.request)
    return result


def may_retry(session: Session, user: User, track_id: str, config: ConfigManager) -> bool:
    """Whether ``user`` may retry the download of ``track_id``. A retry is not
    a request: it runs again a download that exists, and a download exists only
    because a request was approved. So a user the policy governs may retry only
    a track they have a download of (their own, or one they attached to), never
    start one the policy never saw; admins and open mode may retry any."""
    if not governed(user, config):
        return True
    return session.exec(
        select(Download.id).where(Download.track_id == track_id, visible_to_user_clause(user.id)).limit(1)
    ).first() is not None


# ---------------------------------------------------------------------------
# What the API shows
# ---------------------------------------------------------------------------


def public_request(request: DownloadRequest, *, requester: str | None = None, decider: str | None = None,
                   admin: bool = False) -> dict[str, Any]:
    """A request as the API shows it. The admin's view names who asked
    (``requested_by``) and who decided (``decided_by``); the requester's own
    view names nobody."""
    data: dict[str, Any] = {
        "id": request.id,
        "kind": request.kind,
        "status": request.status,
        "auto": request.auto,
        "reason": request.reason,
        "target": {"provider": request.target_provider, "id": request.target_id},
        "title": request.title,
        "subtitle": request.subtitle,
        "cover_url": public_urls.publish(request.cover_url),
        "needed_tracks": request.needed_tracks,
        "queued_tracks": request.queued_tracks,
        "dispatched_to": request.dispatched_to,
        "add_to_favorites": request.add_to_favorites,
        "decision_note": request.decision_note,
        "created_at": utc_iso(request.created_at),
        "decided_at": utc_iso(request.decided_at),
    }
    if admin:
        data["requested_by"] = {"id": request.user_id, "username": requester}
        data["decided_by"] = {"id": request.decided_by, "username": decider} if request.decided_by else None
    return data


def admin_view(session: Session, request: DownloadRequest) -> dict[str, Any]:
    """One request as the admin sees it (with the names of who asked and who
    decided)."""
    names = {
        user.id: user.username
        for user in session.exec(select(User).where(User.id.in_([request.user_id, request.decided_by or ""]))).all()
    }
    return public_request(request, requester=names.get(request.user_id), decider=names.get(request.decided_by), admin=True)


def list_for_user(session: Session, user: User, *, statuses, page: int, limit: int) -> tuple[list[dict], int]:
    rows, total = store.list_requests(session, user_id=user.id, statuses=statuses, page=page, limit=limit)
    return [public_request(request) for request, _ in rows], total


def list_for_admin(
    session: Session, *, statuses, kinds, user_id: str | None, page: int, limit: int,
) -> tuple[list[dict], int]:
    rows, total = store.list_requests(
        session, user_id=user_id, statuses=statuses, kinds=kinds, page=page, limit=limit,
    )
    deciders = {request.decided_by for request, _ in rows if request.decided_by}
    names = {
        user.id: user.username
        for user in session.exec(select(User).where(User.id.in_(deciders))).all()
    } if deciders else {}
    return [
        public_request(request, requester=username, decider=names.get(request.decided_by), admin=True)
        for request, username in rows
    ], total


def policy_status(session: Session, user: User | None, config: ConfigManager) -> dict[str, Any]:
    """What the UI needs to label a download honestly, for ``user`` right now
    (the server stays the only judge at request time): whether the policy
    applies, and per kind the mode, the threshold and ``auto_up_to`` — the
    most tracks a request of that kind can need and still go at once
    (``null`` no limit, ``0`` always waits), with the quota already in it —
    and the quota used and left in the window. ``user`` is ``None`` in open
    mode (no individual users): the policy does not apply."""
    if user is None or not governed(user, config):
        return {"applies": False, "rules": None, "quota": None}
    effective = policy_for(session, config, user.id)
    used = store.tracks_in_window(session, user.id, _now() - timedelta(days=effective.quota_days))
    quota = effective.quota_tracks
    return {
        "applies": True,
        "rules": {
            kind: {
                "mode": rule.mode, "threshold": rule.threshold,
                "auto_up_to": policy.auto_limit(effective, kind, used),
            }
            for kind, rule in effective.rules.items()
        },
        "quota": {
            "tracks": quota,
            "window_days": effective.quota_days,
            "used": used,
            "remaining": max(0, quota - used) if quota > 0 else None,
        },
    }


# ---------------------------------------------------------------------------
# Notifications (the progress stream: ``services/progress.py``)
# ---------------------------------------------------------------------------


def _publish(event: dict) -> None:
    try:
        publish_event(event)
    except Exception as exc:  # noqa: BLE001 — the stream is a nicety, the row is the truth
        logger.debug("download request event not published: %s", exc)


def _notify_requester(event: str, request: DownloadRequest | None) -> None:
    """Tells the requester how their request was decided: an event with their
    ``user_id`` reaches only them (``routes/system/progress.py``)."""
    if request is not None:
        _publish({"type": EVENT_TYPE, "event": event, "user_id": request.user_id, "request": public_request(request)})


def _notify_admins(session: Session, event: str, request: DownloadRequest | None) -> None:
    """Tells the admins a request is waiting, or was decided: an event with no
    ``user_id`` and no download reaches only admins."""
    if request is None:
        return
    requester = session.get(User, request.user_id)
    _publish({
        "type": EVENT_TYPE, "event": event,
        "request": public_request(request, requester=requester.username if requester else None, admin=True),
        "pending": store.count_pending(session),
    })
