"""
Authentication goes from off to on: what the open-mode owner owned becomes an administrator's.

With authentication off the instance has ONE owner, the synthetic user (``SYSTEM_USER_ID``), and
every row of personal data (favorites, likes, playlists, personal settings...) is parented to it.
Turning authentication on makes the synthetic user nobody (``services/credentials.py``), so the
owner's data would be unreachable: ``adopt`` moves it to the administrator the owner chose.

The rules (``docs/permissions.md``, "Turning authentication on"):

- Only the rows that are PERSONAL DATA move (``ADOPTED``); the ones that are sessions or policy of
  a signed-in account do not (``NOT_ADOPTED``, each with its reason). Every foreign key to
  ``users`` is in exactly one of the two: ``tests/test_adoption.py`` fails for a new one nobody
  classified, so a table added later cannot be forgotten.
- A row whose move would break a unique constraint (the administrator already has that favorite,
  that setting, follows that playlist...) is LEFT with the synthetic user, never merged and never
  deleted: the administrator's own row wins, nothing is lost, and the row is the owner's again if
  open mode comes back. Running ``adopt`` again moves what is still movable and nothing else.
- Library job runs move only when finished: a run still queued or running belongs to the job's
  worker, which reports on it under the id it was queued with.
- A download request still pending that the administrator has a pending twin of is cancelled
  (the partial unique index allows one per target), then moves like the others.
- The shared accounts stay shared. The only credentials that move are the owner's accounts of a
  provider whose model is ``personal`` (no shared account at all): in open mode they were stored as
  the shared row (``instance_owner``) and, with authentication on, nobody would read them there.
  A ``personal_with_shared`` account is NOT copied: the shared row keeps serving the administrator
  (and everyone) and the administrator connects their own when they want one.

Nothing here commits: the caller commits it together with the switch (one transaction).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy import and_, exists, func, select, update
from sqlmodel import Session

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
    UserFavorite,
    UserRequestPolicy,
    UserSavedTrack,
)

# Every column that is a foreign key to ``users`` and moves. A row hanging from one of these rows
# (the provider rows of a like, the tracks of a playlist) goes with it.
_ADOPTED_COLUMNS = (
    (UserFavorite, "user_id"),
    (RemovedFavorite, "user_id"),
    (UserSavedTrack, "user_id"),
    (PendingLike, "user_id"),
    (Playlist, "user_id"),
    (PlaylistSync, "user_id"),
    (Setting, "user_id"),  # the scope='user' rows: the owner's personal settings
    (DownloadFollower, "user_id"),
    (Download, "requested_by"),
    (DownloadRequest, "user_id"),
    (DownloadRequest, "decided_by"),
    (LibraryJobRun, "user_id"),
)
ADOPTED: tuple[tuple[str, str], ...] = tuple((model.__tablename__, column) for model, column in _ADOPTED_COLUMNS)

# Why the others stay with the synthetic user.
NOT_ADOPTED: dict[tuple[str, str], str] = {
    (RefreshToken.__tablename__, "user_id"): "a session of a sign-in; the synthetic user never signs in, so it has none",
    (ProviderLoginCode.__tablename__, "user_id"): "a one-time code of a sign-in through a provider; the synthetic user never signs in",
    (UserRequestPolicy.__tablename__, "user_id"): (
        "an admin's override of the download policy for a governed user; nobody is governed in open mode "
        "and an administrator never is"
    ),
    (ProviderCredential.__tablename__, "user_id"): (
        "the synthetic user owns no account row (migration 0042 made them shared); the owner's accounts "
        "move by provider model, see adopt_personal_accounts"
    ),
}

# The credential types a person connects as THEIR account (``AccountTarget.PERSONAL``).
PERSONAL_CREDENTIAL_TYPES = ("oauth2_token", "credentials")
_ACTIVE_JOB = ("queued", "running")


@dataclass
class Adoption:
    """What ``adopt`` did, per ``table.column``: rows moved, and rows left with the synthetic user
    (only those that are left) because the administrator already had their own."""

    administrator_id: str
    moved: dict[str, int] = field(default_factory=dict)
    left: dict[str, int] = field(default_factory=dict)
    cancelled_requests: int = 0
    accounts_moved: int = 0

    @property
    def total_moved(self) -> int:
        return sum(self.moved.values()) + self.accounts_moved


def _key(model, column: str) -> str:
    return f"{model.__tablename__}.{column}"


def _move(
    session: Session, model, column: str, administrator_id: str, *, same: Sequence[str] = (), where: Iterable = (),
) -> int:
    """Re-parents the rows of ``model`` the synthetic user owns through ``column``. ``same``: the
    other columns of the unique constraint the user is part of; a row for which the administrator
    already has one that matches on all of them stays."""
    table = model.__table__
    statement = update(table).where(table.c[column] == SYSTEM_USER_ID, *where).values({column: administrator_id})
    if same:
        twin = table.alias("twin")
        statement = statement.where(~exists().where(and_(
            twin.c[column] == administrator_id, *(twin.c[name] == table.c[name] for name in same),
        )))
    return session.execute(statement).rowcount


def _still_owned(session: Session, model, column: str) -> int:
    table = model.__table__
    return session.execute(select(func.count()).select_from(table).where(table.c[column] == SYSTEM_USER_ID)).scalar_one()


def adopt(session: Session, administrator_id: str, *, personal_only_providers: Iterable[str] = ()) -> Adoption:
    """Moves the personal data of the synthetic user to ``administrator_id`` (see the module).
    ``personal_only_providers``: the providers whose account model is ``personal``, whose owner's
    accounts move too. Idempotent; commits nothing."""
    report = Adoption(administrator_id)
    moved = report.moved

    # Rows with a unique constraint on (user, ...): the administrator's own wins.
    for model, same in (
        (UserFavorite, ("track_id",)),
        (RemovedFavorite, ("key",)),
        (UserSavedTrack, ("track_id",)),
        (PendingLike, ("track_id",)),
        (DownloadFollower, ("download_id",)),
        (Setting, ("key", "scope")),
        # A followed playlist is one per source; an own one has no source (NULLs never match).
        (Playlist, ("source_provider", "source_id")),
    ):
        moved[_key(model, "user_id")] = _move(session, model, "user_id", administrator_id, same=same)

    # The copy of a playlist on a sync provider follows its playlist; the ones whose playlist is
    # gone (the routine still has to delete the copy) follow the user.
    playlists, syncs = Playlist.__table__, PlaylistSync.__table__
    kept_with_system = exists().where(and_(playlists.c.id == syncs.c.playlist_id, playlists.c.user_id == SYSTEM_USER_ID))
    moved[_key(PlaylistSync, "user_id")] = _move(session, PlaylistSync, "user_id", administrator_id, where=[~kept_with_system])

    moved[_key(Download, "requested_by")] = _move(session, Download, "requested_by", administrator_id)

    # One pending request per user and target: the administrator's own is kept, the other cancelled.
    requests = DownloadRequest.__table__
    twin = requests.alias("twin")
    report.cancelled_requests = session.execute(
        update(requests)
        .where(requests.c.user_id == SYSTEM_USER_ID, requests.c.status == "pending")
        .where(exists().where(and_(
            twin.c.user_id == administrator_id, twin.c.status == "pending", twin.c.kind == requests.c.kind,
            twin.c.target_provider == requests.c.target_provider, twin.c.target_id == requests.c.target_id,
        )))
        .values(status="cancelled")
    ).rowcount
    moved[_key(DownloadRequest, "user_id")] = _move(session, DownloadRequest, "user_id", administrator_id)
    moved[_key(DownloadRequest, "decided_by")] = _move(session, DownloadRequest, "decided_by", administrator_id)

    jobs = LibraryJobRun.__table__
    moved[_key(LibraryJobRun, "user_id")] = _move(
        session, LibraryJobRun, "user_id", administrator_id, where=[jobs.c.status.notin_(_ACTIVE_JOB)],
    )

    report.accounts_moved = adopt_personal_accounts(session, administrator_id, personal_only_providers)

    report.moved = {key: count for key, count in moved.items() if count}
    report.left = {
        _key(model, column): left for model, column in _ADOPTED_COLUMNS if (left := _still_owned(session, model, column))
    }
    return report


def adopt_personal_accounts(session: Session, administrator_id: str, providers: Iterable[str]) -> int:
    """The owner's accounts of the providers with ``personal`` accounts only (no shared one): in
    open mode they were stored as the shared row (``db.auth_store.CredentialStore``,
    ``instance_owner``), which with authentication on nobody reads. They become the administrator's
    own (the administrator's existing account of the same provider wins). The row moves as it is
    stored, sealed. The shared accounts of every other provider are not touched."""
    names = sorted(set(providers))
    if not names:
        return 0
    table = ProviderCredential.__table__
    twin = table.alias("twin")
    return session.execute(
        update(table)
        .where(
            table.c.user_id.is_(None), table.c.provider.in_(names), table.c.credential_type.in_(PERSONAL_CREDENTIAL_TYPES),
            ~exists().where(and_(
                twin.c.user_id == administrator_id, twin.c.provider == table.c.provider,
                twin.c.credential_type == table.c.credential_type,
            )),
        )
        .values(user_id=administrator_id, external_id=None)
    ).rowcount
