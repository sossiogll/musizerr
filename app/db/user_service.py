"""Data-access layer for User and RefreshToken models."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .models import (
    SYSTEM_USER_ID,
    Download,
    DownloadFollower,
    DownloadRequest,
    LibraryJobRun,
    PendingLike,
    Playlist,
    PlaylistSync,
    ProviderCredential,
    ProviderLoginCode,
    RefreshToken,
    RemovedFavorite,
    Setting,
    User,
    UserFavorite,
    UserRequestPolicy,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from . import playlist_store
from .upsert import insert_ignore

# What a user owns: it goes with the account (``delete_user``). The columns
# that only remember who asked (``Download.requested_by``) keep their row, with
# no requester. ``tests/test_user_service.py`` checks that every foreign key
# to ``users`` is in one of the two.
ACCOUNT_DATA = (
    DownloadFollower, DownloadRequest, LibraryJobRun, PendingLike, Playlist, PlaylistSync, ProviderCredential,
    ProviderLoginCode, RefreshToken, RemovedFavorite, Setting, UserFavorite, UserRequestPolicy, UserSavedTrack,
)
KEPT_WITHOUT_USER = (Download.requested_by, DownloadRequest.decided_by)


# ------------------------------------------------------------------
# User CRUD
# ------------------------------------------------------------------

def get_user_by_id(session: Session, user_id: str) -> User | None:
    return session.get(User, user_id)


def get_user_by_username(session: Session, username: str) -> User | None:
    return session.exec(select(User).where(User.username == username, User.id != SYSTEM_USER_ID)).first()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.exec(select(User).where(User.email == email)).first()


def get_user_count(session: Session) -> int:
    return len(list_users(session))


def list_users(session: Session) -> list[User]:
    """The accounts (not the system user, ``ensure_system_user``)."""
    return list(session.exec(select(User).where(User.id != SYSTEM_USER_ID)).all())


def system_user() -> User:
    """The row of the user that the rows written with no sign-in belong to
    (``SYSTEM_USER_ID``: an instance with authentication off keeps favorites
    and likes that point at a user like any other). It is not an account:
    nobody can sign in as it, and it is neither listed nor counted."""
    return User(id=SYSTEM_USER_ID, username=SYSTEM_USER_ID, password_hash="", role="admin", is_active=False)


def ensure_system_user(session: Session) -> None:
    """The row of ``system_user`` exists. Idempotent."""
    insert_ignore(session, system_user())
    session.commit()


def create_user(
    session: Session,
    *,
    username: str,
    password_hash: str,
    role: str = "user",
    email: Optional[str] = None,
) -> User:
    user = User(
        username=username,
        email=email,
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def username_taken(session: Session, username: str) -> bool:
    return get_user_by_username(session, username) is not None or username == SYSTEM_USER_ID


class AccountExists(Exception):
    """The username or the email of a new account is already used. ``field`` says which."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{field} already taken")


def create_account(
    session: Session,
    *,
    username: str,
    password_hash: str,
    email: Optional[str] = None,
    role: str = "user",
) -> User:
    """A new account, the one way a person's account is made (registration and Administration ›
    Users): the username (the synthetic user's id included) and the email must be free, and the
    unique constraints decide a race (``AccountExists``, the session rolled back)."""
    if username_taken(session, username):
        raise AccountExists("username")
    if email and get_user_by_email(session, email):
        raise AccountExists("email")
    try:
        return create_user(session, username=username, password_hash=password_hash, email=email, role=role)
    except IntegrityError:  # the same username or email was registered meanwhile
        session.rollback()
        raise AccountExists("username or email") from None


def administrators(session: Session) -> list[User]:
    """The administrators a person can sign in as, oldest first: active, approved, with a password.
    The synthetic user is not one (its row is never active and has no password), nor an account
    waiting for approval."""
    return list(session.exec(_signable_administrators(select(User)).order_by(User.created_at, User.id)).all())


def has_usable_administrator(session: Session) -> bool:
    """Whether someone can sign in as an administrator (``administrators``). The check that
    authentication may be turned on (``core.settings_catalog``, ``auth_enabled``)."""
    return session.exec(_signable_administrators(select(User.id)).limit(1)).first() is not None


def _signable_administrators(statement):
    return statement.where(
        User.role == "admin", User.is_active == True, User.pending_approval == False,  # noqa: E712
        User.id != SYSTEM_USER_ID, User.password_hash != "",
    )


def update_user(session: Session, user: User, **fields) -> User:
    for k, v in fields.items():
        setattr(user, k, v)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def delete_saved_tracks(session: Session, user_id: str) -> None:
    """The likes a user imported from other providers, with their per-provider
    rows. Nothing is committed."""
    saved = select(UserSavedTrack.id).where(UserSavedTrack.user_id == user_id)
    session.exec(delete(UserSavedTrackProvider).where(UserSavedTrackProvider.saved_track_id.in_(saved)))
    session.exec(delete(UserSavedTrack).where(UserSavedTrack.user_id == user_id))


def delete_user(session: Session, user: User) -> None:
    """The account and what it owns (``ACCOUNT_DATA``); the downloads it
    requested and the requests it decided stay, with nobody named."""
    delete_saved_tracks(session, user.id)  # first: their provider rows point at them
    playlist_store.delete_tracks_of_user(session, user.id)  # first: they point at the playlists
    for model in ACCOUNT_DATA:
        session.exec(delete(model).where(model.user_id == user.id))
    for column in KEPT_WITHOUT_USER:
        session.exec(update(column.class_).where(column == user.id).values({column.key: None}))
    session.delete(user)
    session.commit()


# ------------------------------------------------------------------
# Refresh tokens
# ------------------------------------------------------------------

def store_refresh_token(
    session: Session,
    *,
    user_id: str,
    token_hash: str,
    expires_at: datetime,
) -> RefreshToken:
    """The first token of a sign-in: it starts its own family."""
    rt = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    rt.family_id = rt.id
    session.add(rt)
    session.commit()
    return rt


def find_refresh_token(session: Session, token_hash: str) -> RefreshToken | None:
    """The token with this hash, whether active, rotated or revoked."""
    return session.exec(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).first()


def rotate_refresh_token(
    session: Session,
    rt: RefreshToken,
    *,
    successor_hash: str,
    successor_expires_at: datetime,
    now: datetime,
) -> bool:
    """Replaces the active ``rt`` with its successor, in one transaction.

    The claim is a conditional UPDATE (``revoked_at IS NULL``): of two
    requests rotating the same token at once, exactly one wins and inserts
    the successor; the other gets False (and nothing written) and treats the
    token as already rotated."""
    successor = RefreshToken(
        user_id=rt.user_id,
        token_hash=successor_hash,
        expires_at=successor_expires_at,
        family_id=rt.family_id or rt.id,
    )
    claimed = session.exec(
        update(RefreshToken)
        .where(RefreshToken.id == rt.id, RefreshToken.revoked_at == None)  # noqa: E711
        .values(revoked_at=now, replaced_by_id=successor.id)
    )
    if claimed.rowcount != 1:
        session.rollback()
        return False
    session.add(successor)
    session.commit()
    return True


def revoke_refresh_family(session: Session, family_id: str, *, now: datetime) -> int:
    """Revokes every still-active token of a sign-in; the count revoked."""
    result = session.exec(
        update(RefreshToken)
        .where(
            or_(RefreshToken.family_id == family_id, RefreshToken.id == family_id),
            RefreshToken.revoked_at == None,  # noqa: E711
        )
        .values(revoked_at=now)
    )
    session.commit()
    return result.rowcount


def revoke_refresh_token(session: Session, token_hash: str) -> bool:
    rt = session.exec(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).first()
    if not rt:
        return False
    if rt.revoked_at is not None:
        # Already rotated or revoked: its time decides the grace window.
        return True
    rt.revoked_at = datetime.now(timezone.utc)
    session.add(rt)
    session.commit()
    return True


# ------------------------------------------------------------------
# One-time codes of a sign-in through a provider
# ------------------------------------------------------------------

def store_login_code(session: Session, *, user_id: str, code_hash: str, expires_at: datetime, now: datetime) -> None:
    """Remembers the code a sign-in handed the browser. The codes already
    expired are dropped here: nothing else ever reads them again."""
    session.exec(delete(ProviderLoginCode).where(ProviderLoginCode.expires_at < now))
    session.add(ProviderLoginCode(user_id=user_id, code_hash=code_hash, expires_at=expires_at))
    session.commit()


def claim_login_code(session: Session, code_hash: str, *, now: datetime) -> str | None:
    """The user the code was issued for, and the code is spent: ONE conditional
    UPDATE (``used_at IS NULL`` and not expired), so of two exchanges of the
    same code exactly one wins. None when the code is unknown, used or expired."""
    claimed = session.exec(
        update(ProviderLoginCode)
        .where(
            ProviderLoginCode.code_hash == code_hash,
            ProviderLoginCode.used_at == None,  # noqa: E711
            ProviderLoginCode.expires_at > now,
        )
        .values(used_at=now)
    )
    session.commit()
    if claimed.rowcount != 1:
        return None
    return session.exec(select(ProviderLoginCode.user_id).where(ProviderLoginCode.code_hash == code_hash)).first()
