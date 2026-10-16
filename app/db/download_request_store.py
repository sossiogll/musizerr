"""
Data access of the download requests (``DownloadRequest``) and of the per-user
policy overrides (``UserRequestPolicy``): the rules of the policy itself are in
``core/download_policy.py`` and the use cases in ``services/download_requests.py``.

A request leaves ``pending`` exactly once. ``claim`` is the one gate: a
conditional UPDATE (``WHERE status = 'pending'``) that exactly one of any
number of concurrent callers wins, on SQLite and PostgreSQL alike (the second
UPDATE waits for the first one's row lock and then finds the row no longer
pending). Whoever wins runs the consequences; the others do nothing.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import func, update
from sqlalchemy.orm import defer
from sqlmodel import Session, select

from core import download_policy as policy
from db.models import DownloadRequest, User, UserRequestPolicy
from db.upsert import upsert


def get(session: Session, request_id: str) -> DownloadRequest | None:
    return session.get(DownloadRequest, request_id)


def find_pending(session: Session, user_id: str, kind: str, provider: str, target_id: str) -> DownloadRequest | None:
    """The user's pending request for this very target (they have at most one)."""
    return session.exec(
        select(DownloadRequest).where(
            DownloadRequest.user_id == user_id,
            DownloadRequest.status == policy.STATUS_PENDING,
            DownloadRequest.kind == kind,
            DownloadRequest.target_provider == provider,
            DownloadRequest.target_id == target_id,
        )
    ).first()


def tracks_in_window(session: Session, user_id: str, since: datetime) -> int:
    """The tracks the user's approved requests queued since ``since``: what
    each queued (``queued_tracks``) or, while it is still being queued, what
    it needed (``needed_tracks``: the quota is reserved at the decision, so
    two requests decided together never both fit under the same room)."""
    total = session.exec(
        select(func.coalesce(func.sum(func.coalesce(DownloadRequest.queued_tracks, DownloadRequest.needed_tracks)), 0))
        .where(
            DownloadRequest.user_id == user_id,
            DownloadRequest.status == policy.STATUS_APPROVED,
            DownloadRequest.decided_at >= since,
        )
    ).one()
    return int(total)


def claim(
    session: Session,
    request_id: str,
    to_status: str,
    *,
    decided_by: str,
    decided_at: datetime,
    note: Optional[str] = None,
    requester: Optional[str] = None,
) -> bool:
    """Moves a PENDING request to ``to_status``, atomically: True for the one
    caller that did it, False when the request is not pending (decided by
    someone else, or gone). ``requester`` restricts the claim to that user's
    own request (a user cancelling)."""
    statement = (
        update(DownloadRequest)
        .where(DownloadRequest.id == request_id, DownloadRequest.status == policy.STATUS_PENDING)
        .values(status=to_status, decided_by=decided_by, decided_at=decided_at, decision_note=note)
    )
    if requester is not None:
        statement = statement.where(DownloadRequest.user_id == requester)
    claimed = session.exec(statement)
    session.commit()
    return claimed.rowcount == 1


def reopen(session: Session, request_id: str, decided_by: str) -> bool:
    """Puts back to ``pending`` a request that ``decided_by`` approved and
    that queued nothing (``queued_tracks`` still NULL): approval failed before
    any track went in, so the admin can try again."""
    reopened = session.exec(
        update(DownloadRequest)
        .where(
            DownloadRequest.id == request_id,
            DownloadRequest.status == policy.STATUS_APPROVED,
            DownloadRequest.decided_by == decided_by,
            DownloadRequest.queued_tracks.is_(None),  # type: ignore[union-attr]
        )
        .values(status=policy.STATUS_PENDING, decided_by=None, decided_at=None, decision_note=None)
    )
    session.commit()
    return reopened.rowcount == 1


def record_outcome(session: Session, request_id: str, *, queued: int, dispatched_to: str | None) -> None:
    """What an approved request queued (the quota counts it from now on)."""
    session.exec(
        update(DownloadRequest).where(DownloadRequest.id == request_id)
        .values(queued_tracks=queued, dispatched_to=dispatched_to)
    )
    session.commit()


def delete(session: Session, request_id: str) -> None:
    """A request that never took effect (its execution failed before queueing anything)."""
    request = session.get(DownloadRequest, request_id)
    if request is not None:
        session.delete(request)
        session.commit()


def _filtered(query, *, user_id: str | None, statuses: Iterable[str], kinds: Iterable[str]):
    if user_id is not None:
        query = query.where(DownloadRequest.user_id == user_id)
    statuses, kinds = list(statuses), list(kinds)
    if statuses:
        query = query.where(DownloadRequest.status.in_(statuses))  # type: ignore[union-attr]
    if kinds:
        query = query.where(DownloadRequest.kind.in_(kinds))  # type: ignore[union-attr]
    return query


def list_requests(
    session: Session,
    *,
    user_id: str | None = None,
    statuses: Iterable[str] = (),
    kinds: Iterable[str] = (),
    page: int = 1,
    limit: int = 50,
) -> tuple[list[tuple[DownloadRequest, str | None]], int]:
    """A page of requests, newest first, each with its requester's username,
    and the total. The payload (the tracks) is not loaded."""
    total = session.exec(_filtered(select(func.count(DownloadRequest.id)), user_id=user_id, statuses=statuses, kinds=kinds)).one()
    rows = session.exec(
        _filtered(
            select(DownloadRequest, User.username)
            .join(User, User.id == DownloadRequest.user_id, isouter=True)
            .options(defer(DownloadRequest.payload)),  # type: ignore[arg-type]
            user_id=user_id, statuses=statuses, kinds=kinds,
        )
        .order_by(DownloadRequest.created_at.desc(), DownloadRequest.id)  # type: ignore[union-attr]
        .offset((page - 1) * limit).limit(limit)
    ).all()
    return [(request, username) for request, username in rows], int(total)


def count_pending(session: Session) -> int:
    return int(session.exec(
        select(func.count(DownloadRequest.id)).where(DownloadRequest.status == policy.STATUS_PENDING)
    ).one())


# ---------------------------------------------------------------------------
# Per-user policy overrides
# ---------------------------------------------------------------------------


def get_override(session: Session, user_id: str) -> policy.PolicyOverride:
    """What an admin changed for the user (empty when nothing)."""
    row = session.get(UserRequestPolicy, user_id)
    if row is None:
        return policy.PolicyOverride()
    return policy.PolicyOverride.from_dict({"rules": json.loads(row.rules or "{}"), "quota_tracks": row.quota_tracks})


def set_override(session: Session, user_id: str, override: policy.PolicyOverride, at: datetime) -> None:
    """Replaces the user's override with ``override`` (one writer wins)."""
    data = override.as_dict()
    upsert(
        session,
        UserRequestPolicy(user_id=user_id, rules=json.dumps(data["rules"]), quota_tracks=data["quota_tracks"], updated_at=at),
        conflict=("user_id",), update=("rules", "quota_tracks", "updated_at"),
    )
    session.commit()


def clear_override(session: Session, user_id: str) -> None:
    row = session.get(UserRequestPolicy, user_id)
    if row is not None:
        session.delete(row)
        session.commit()
