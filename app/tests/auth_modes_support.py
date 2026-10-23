"""Shared by the tests of the two authentication modes and of the switch between them: an instance
(a database, its settings, the whole API) that can be open or closed, accounts with a session each,
and the personal data an open-mode owner leaves behind. Fakes only, no real provider."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select as core_select
from sqlmodel import Session, SQLModel, select

import main
from core.user_auth import create_access_token, hash_password
from db.engine import get_session
from db.models import (
    SYSTEM_USER_ID,
    Download,
    DownloadFollower,
    DownloadRequest,
    LibraryJobRun,
    PendingLike,
    Playlist,
    PlaylistSync,
    PlaylistTrack,
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
from routes import deps
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine, seed_parents, seed_system_user

LONG_ENOUGH = "correct-horse-battery"
STRONG_SECRET = "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1"


class World:
    """An instance and its API. ``open``/``close`` switch ``auth_enabled`` straight in the database
    (what a settings write ends in), the way a test sets up a state it did not get to by the API."""

    def __init__(self, engine, registry: FakeRegistry | None = None) -> None:
        self.engine = engine
        self.config = ConfigManager(engine=engine)
        self.registry = registry or FakeRegistry()
        self.client = TestClient(main.app)

    # -- the mode -------------------------------------------------------

    def set_authentication(self, enabled: bool, **settings) -> None:
        self.config.update({"auth_enabled": enabled, **settings})

    # -- accounts -------------------------------------------------------

    def account(
        self, name: str, *, role: str = "user", active: bool = True, password: str | None = LONG_ENOUGH,
        pending: bool = False, created_at: datetime | None = None,
    ) -> User:
        user = User(
            username=name, role=role, is_active=active, pending_approval=pending,
            password_hash=hash_password(password) if password is not None else "",
        )
        if created_at is not None:
            user.created_at = created_at
        with Session(self.engine) as session:
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

    def headers(self, user: User) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    def session(self) -> Session:
        return Session(self.engine)


@pytest.fixture
def world(monkeypatch):
    """A closed-by-default API on a database of its own, with the synthetic user's row in place."""
    monkeypatch.setenv("AUTH_SECRET", STRONG_SECRET)
    engine = new_engine()
    seed_system_user(engine)
    instance = World(engine)

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: instance.config,
        deps.get_registry: lambda: instance.registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    try:
        yield instance
    finally:
        for key in overrides:
            main.app.dependency_overrides.pop(key, None)


# ----------------------------------------------------------------------------
# The personal data of an account
# ----------------------------------------------------------------------------

def owner_data(session: Session, owner: str, *, tag: str = "o", shared_with: str | None = None) -> dict[str, str]:
    """One row of everything an account owns that adoption moves, for ``owner``. Names and tracks
    are built from ``tag``, so two owners' rows differ; with ``shared_with`` (another owner's tag)
    the rows that sit under a unique constraint are the SAME as that owner's. Returns the ids
    a test looks at again."""
    same = shared_with or tag
    tracks = [f"{same}-t1", f"{same}-t2", f"{same}-t3", f"{tag}-t4"]
    seed_parents(session, tracks=tracks, users=[owner])
    ids: dict[str, str] = {}

    session.add(UserFavorite(user_id=owner, track_id=tracks[0]))
    session.add(RemovedFavorite(user_id=owner, key=f"song:{same}", group_id=f"g-{tag}"))
    saved = UserSavedTrack(user_id=owner, track_id=tracks[1])
    session.add(saved)
    session.add(PendingLike(user_id=owner, track_id=tracks[2]))
    session.add(Setting(key="theme", scope="user", user_id=owner, value=f'"{tag}-theme"'))
    session.flush()
    session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider="p", provider_track_id=f"{tag}-x"))
    ids["saved"] = saved.id

    own = Playlist(user_id=owner, name=f"{tag} mix")
    followed = Playlist(user_id=owner, name=f"{tag} followed", kind="followed", source_provider="catalog", source_id=f"src-{same}")
    session.add_all([own, followed])
    session.flush()
    session.add_all([
        PlaylistTrack(playlist_id=own.id, track_id=tracks[3], position=0),
        PlaylistTrack(playlist_id=followed.id, track_id=tracks[0], position=0),
        PlaylistSync(playlist_id=own.id, sync_provider="sync", user_id=owner, remote_id=f"{tag}-own"),
        PlaylistSync(playlist_id=followed.id, sync_provider="sync", user_id=owner, remote_id=f"{tag}-followed"),
        # The copy of a playlist that no longer exists: the routine still has to delete it remotely.
        PlaylistSync(playlist_id=f"{tag}-deleted", sync_provider="sync", user_id=owner, remote_id=f"{tag}-gone"),
    ])
    ids["own_playlist"], ids["followed_playlist"] = own.id, followed.id

    mine = Download(track_id=tracks[3], requested_by=owner, status="downloaded")
    theirs = Download(track_id=tracks[0], requested_by=None, status="downloaded")
    session.add_all([mine, theirs])
    session.flush()
    session.add(DownloadFollower(download_id=theirs.id, user_id=owner))
    ids["download"], ids["followed_download"] = mine.id, theirs.id

    request = DownloadRequest(user_id=owner, kind="track", target_provider="p", target_id=f"{tag}-done", status="approved", decided_by=owner)
    pending = DownloadRequest(user_id=owner, kind="album", target_provider="p", target_id=f"{same}-album", status="pending")
    session.add_all([request, pending])
    ids["request"], ids["pending_request"] = request.id, pending.id

    done = LibraryJobRun(user_id=owner, kind="library_sync", status="completed")
    active = LibraryJobRun(user_id=owner, kind="download_sync", status="running")
    session.add_all([done, active])
    ids["finished_job"], ids["active_job"] = done.id, active.id
    session.commit()
    return ids


def sessions_of_the_synthetic_user(session: Session) -> None:
    """What the synthetic user keeps whatever happens: a session artifact and an admin's override."""
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    session.add(RefreshToken(user_id=SYSTEM_USER_ID, token_hash="system-refresh", expires_at=soon))
    session.add(ProviderLoginCode(user_id=SYSTEM_USER_ID, code_hash="system-code", expires_at=soon))
    session.add(UserRequestPolicy(user_id=SYSTEM_USER_ID))
    session.commit()


def count_owned(session: Session, table: str, column: str, user_id: str) -> int:
    """How many rows of ``table`` name ``user_id`` in ``column``."""
    meta = SQLModel.metadata.tables[table]
    return session.execute(core_select(func.count()).select_from(meta).where(meta.c[column] == user_id)).scalar_one()


def owned_by(session: Session, model, user_id: str, column: str = "user_id") -> list:
    return list(session.exec(select(model).where(getattr(model, column) == user_id)).all())
