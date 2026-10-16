"""
Rotation of the refresh tokens, with a short grace window.

Every refresh replaces the presented token with its successor. All the tabs
of a browser share one refresh token, so two of them can present the same
token at nearly the same moment: the first rotates it, and the second would
find it already revoked and sign the user out. Within
``REUSE_GRACE_SECONDS`` of the rotation, presenting the rotated token again
answers the SAME successor (``core.user_auth.successor_refresh_token``
derives it, nothing is stored in clear) and a fresh access token.

Outside the window, a rotated token coming back is real reuse (a copy of the
token is in someone else's hands): the whole family of the sign-in is
revoked and every holder must sign in again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session

from core import user_auth
from core.utc import as_utc
from db import user_service
from db.models import RefreshToken, User

logger = logging.getLogger(__name__)

# Long enough for the requests of every tab reloaded at once to arrive
# (a request can wait in the browser queue for the whole client timeout),
# short enough that a stolen rotated token is worthless soon after.
REUSE_GRACE_SECONDS = 60
# Hops followed when the successor was itself rotated within the window.
_MAX_CHAIN = 3


class RefreshRefused(Exception):
    """The token cannot be refreshed; ``reason`` says why (for the 401)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RefreshedSession:
    access_token: str
    refresh_token: str


def start_session(session: Session, user: User) -> RefreshedSession:
    """A new sign-in of ``user``: an access token and the first refresh token of a
    family of its own. The ONE place a sign-in is issued, whatever proved who
    the user is (a password, an account linked on a provider)."""
    access = user_auth.create_access_token(user.id, user.role)
    raw_refresh, token_hash, expires_at = user_auth.create_refresh_token()
    user_service.store_refresh_token(session, user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    return RefreshedSession(access, raw_refresh)


def _active_user(session: Session, rt: RefreshToken) -> User:
    user = user_service.get_user_by_id(session, rt.user_id)
    if not user or not user.is_active:
        raise RefreshRefused("User not found or inactive")
    return user


def refresh_session(session: Session, raw: str, *, now: datetime | None = None) -> RefreshedSession:
    """The new pair for the refresh token ``raw``; raises ``RefreshRefused``."""
    now = now or datetime.now(timezone.utc)
    rt = user_service.find_refresh_token(session, user_auth.hash_refresh_token(raw))
    if rt is None:
        raise RefreshRefused("Invalid or revoked refresh token")
    if as_utc(rt.expires_at) < now:
        raise RefreshRefused("Refresh token expired")
    user = _active_user(session, rt)

    if rt.revoked_at is None:
        successor, successor_hash, expires_at = user_auth.successor_refresh_token(raw)
        if user_service.rotate_refresh_token(
            session, rt, successor_hash=successor_hash, successor_expires_at=expires_at, now=now,
        ):
            return RefreshedSession(user_auth.create_access_token(user.id, user.role), successor)
        # Another request rotated it a moment ago: answer as a replay.
        session.expire_all()
        rt = user_service.find_refresh_token(session, rt.token_hash)
    return _replay(session, rt, raw, user, now)


def _replay(session: Session, rt: RefreshToken, raw: str, user: User, now: datetime) -> RefreshedSession:
    """``rt`` is no longer active: the same successor if it was rotated within
    the window (following a short chain of rotations), else a refusal."""
    for _ in range(_MAX_CHAIN):
        if rt.replaced_by_id is None:
            # Revoked by a sign-out or a family revocation: never replayed.
            raise RefreshRefused("Invalid or revoked refresh token")
        family = rt.family_id or rt.id
        age = (now - as_utc(rt.revoked_at)).total_seconds()
        if age > REUSE_GRACE_SECONDS:
            revoked = user_service.revoke_refresh_family(session, family, now=now)
            logger.warning(
                "refresh token reused %.0fs after its rotation: family %s revoked (%d active tokens), user %s",
                age, family, revoked, rt.user_id,
            )
            raise RefreshRefused("Invalid or revoked refresh token")
        raw, successor_hash, _ = user_auth.successor_refresh_token(raw)
        successor = user_service.find_refresh_token(session, successor_hash)
        if successor is None or successor.id != rt.replaced_by_id:
            raise RefreshRefused("Invalid or revoked refresh token")
        if successor.revoked_at is None:
            logger.info("refresh token replayed %.1fs after its rotation (grace window), user %s", age, rt.user_id)
            return RefreshedSession(user_auth.create_access_token(user.id, user.role), raw)
        rt = successor
    raise RefreshRefused("Invalid or revoked refresh token")
