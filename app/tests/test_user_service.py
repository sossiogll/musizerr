"""
``db/user_service.py``: deleting an account takes what it owns with it (a
foreign key would refuse otherwise), and the user that an instance with
authentication off writes its rows as is a row like any other that is not an
account.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from core import download_states as ds
from db import user_service
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
    ProviderCredential,
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
from routes.admin import _managed_user
from routes.users_auth import router as account_router
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import seed_parents


def _account_data(session: Session, user_id: str) -> None:
    """One row of everything an account owns."""
    seed_parents(session, tracks=[f"track-{user_id}"])
    track = f"track-{user_id}"
    download = Download(track_id=track, requested_by=user_id, status=ds.DOWNLOADED)
    session.add(download)
    saved = UserSavedTrack(user_id=user_id, track_id=track)
    session.add(saved)
    playlist = Playlist(id=f"playlist-{user_id}", user_id=user_id, name="Mine")
    session.add(playlist)
    session.flush()
    session.add_all([
        PlaylistTrack(playlist_id=playlist.id, track_id=track),
        PlaylistSync(playlist_id=playlist.id, sync_provider="p", user_id=user_id, remote_id="r"),
        UserSavedTrackProvider(saved_track_id=saved.id, provider="p", provider_track_id="x"),
        DownloadFollower(download_id=download.id, user_id=user_id),
        DownloadRequest(user_id=user_id, kind="track"),
        UserRequestPolicy(user_id=user_id),
        LibraryJobRun(user_id=user_id, kind="library_sync", status="completed"),
        PendingLike(user_id=user_id, track_id=track),
        ProviderCredential(user_id=user_id, provider="p", credential_type="api_key", data=json.dumps({})),
        RefreshToken(user_id=user_id, token_hash=f"hash-{user_id}", expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
        ProviderLoginCode(user_id=user_id, code_hash=f"code-{user_id}", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)),
        RemovedFavorite(user_id=user_id, key="song:x|y", group_id="g"),
        Setting(key="theme", scope="user", user_id=user_id, value='"dark"'),
        UserFavorite(user_id=user_id, track_id=track),
    ])
    session.commit()


def test_every_foreign_key_to_a_user_is_handled_by_the_deletion():
    pointing = {
        (table.name, fk.parent.name)
        for table in SQLModel.metadata.tables.values() for fk in table.foreign_keys if fk.column.table.name == "users"
    }
    handled = {(model.__tablename__, "user_id") for model in user_service.ACCOUNT_DATA}
    handled |= {(column.table.name, column.name) for column in user_service.KEPT_WITHOUT_USER}

    assert pointing == handled


def test_deleting_an_account_deletes_what_it_owns_and_keeps_the_downloads(engine):
    with Session(engine) as session:
        seed_parents(session, users=["gone", "stays"])
        _account_data(session, "gone")
        _account_data(session, "stays")

        user_service.delete_user(session, session.get(User, "gone"))

        assert session.get(User, "gone") is None
        for model in user_service.ACCOUNT_DATA:
            assert session.exec(select(model).where(model.user_id == "gone")).all() == [], model.__name__
            assert len(session.exec(select(model).where(model.user_id == "stays")).all()) == 1, model.__name__
        assert [t.playlist_id for t in session.exec(select(PlaylistTrack)).all()] == ["playlist-stays"]
        assert session.exec(select(UserSavedTrackProvider)).all() and len(
            session.exec(select(UserSavedTrackProvider)).all()) == 1
        downloads = {d.track_id: d.requested_by for d in session.exec(select(Download)).all()}
        assert downloads == {"track-gone": None, "track-stays": "stays"}


def test_the_requests_an_account_decided_stay_with_nobody_named(engine):
    with Session(engine) as session:
        seed_parents(session, users=["admin", "asker"])
        session.add(DownloadRequest(user_id="asker", kind="track", status="approved", decided_by="admin"))
        session.commit()

        user_service.delete_user(session, session.get(User, "admin"))

        request = session.exec(select(DownloadRequest)).one()
        assert (request.user_id, request.status, request.decided_by) == ("asker", "approved", None)


def test_the_likes_of_a_user_are_wiped_with_their_provider_rows(engine):
    with Session(engine) as session:
        seed_parents(session, users=["u"])
        _account_data(session, "u")

        user_service.delete_saved_tracks(session, "u")
        session.commit()

        assert session.exec(select(UserSavedTrack)).all() == []
        assert session.exec(select(UserSavedTrackProvider)).all() == []
        assert len(session.exec(select(UserFavorite)).all()) == 1  # the favorites stay


# ---------------------------------------------------------------------------
# The system user
# ---------------------------------------------------------------------------


def test_the_system_user_is_a_row_that_is_not_an_account(engine):
    with Session(engine) as session:
        user_service.create_user(session, username="alice", password_hash="x")

        user_service.ensure_system_user(session)
        user_service.ensure_system_user(session)  # idempotent

        assert session.get(User, SYSTEM_USER_ID) is not None
        assert [u.username for u in user_service.list_users(session)] == ["alice"]
        assert user_service.get_user_count(session) == 1
        assert user_service.get_user_by_username(session, SYSTEM_USER_ID) is None


def test_the_system_user_cannot_be_managed_by_an_admin(engine):
    with Session(engine) as session:
        user_service.ensure_system_user(session)

        with pytest.raises(HTTPException) as refused:
            _managed_user(session, SYSTEM_USER_ID)

    assert refused.value.status_code == 404


def test_the_rows_of_an_instance_without_authentication_have_a_user_to_point_at(engine):
    with Session(engine) as session:
        seed_parents(session, tracks=["t"])
        user_service.ensure_system_user(session)

        session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id="t"))
        session.add(PendingLike(user_id=SYSTEM_USER_ID, track_id="t"))
        session.commit()


# ---------------------------------------------------------------------------
# Registering the same account twice at once
# ---------------------------------------------------------------------------


def test_a_username_taken_between_the_check_and_the_insert_is_a_409(engine, monkeypatch):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "registration_open": True})
    with Session(engine) as session:
        user_service.create_user(session, username="alice", password_hash="x")
    # The check ran before the other registration committed: it finds nobody.
    monkeypatch.setattr(user_service, "get_user_by_username", lambda session, username: None)
    app = FastAPI()
    app.include_router(account_router, prefix="/auth/account")

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()

    response = TestClient(app).post("/auth/account/register", json={"username": "alice", "password": "long-enough-pw"})

    assert response.status_code == 409
    assert "already taken" in response.json()["detail"]
    with pytest.raises(IntegrityError), Session(engine) as session:
        user_service.create_user(session, username="alice", password_hash="x")
