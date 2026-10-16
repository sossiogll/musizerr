"""
``POST /downloads/retry-failed``: ritenta le tracce fallite che chi chiama
vede in ``GET /downloads``, con la stessa regola del retry singolo; una
seconda chiamata non accoda nulla. Più le chiavi stabili delle aree e dei
gruppi delle impostazioni.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import settings_catalog as sc
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, TrackRecord, User
from routes import deps
from tests.conftest import FakeRegistry
from routes.history import router as history_router
from services import downloads
from services.config_manager import ConfigManager


def _user(engine, name, role="user") -> User:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _track_with(engine, *statuses, requested_by=None) -> str:
    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        for status in statuses:
            session.add(Download(track_id=record.internal_id, status=status, requested_by=requested_by))
        session.commit()
        return record.internal_id


def _client(engine, monkeypatch, *, auth_enabled=True):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": auth_enabled})
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    queued: list = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    app = FastAPI()
    app.include_router(history_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    return TestClient(app), queued


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


def test_only_failed_tracks_are_queued_and_a_second_call_queues_nothing(engine, monkeypatch):
    alice = _user(engine, "alice")
    failed = _track_with(engine, "failed", requested_by=alice.id)
    _track_with(engine, "downloaded", requested_by=alice.id)
    _track_with(engine, "failed", "downloaded", requested_by=alice.id)  # poi riuscita: non è fallita
    _track_with(engine, "downloading", requested_by=alice.id)
    client, queued = _client(engine, monkeypatch)

    first = client.post("/downloads/retry-failed", headers=_auth(alice)).json()
    second = client.post("/downloads/retry-failed", headers=_auth(alice)).json()

    assert len(first["download_ids"]) == 1
    assert [call["track_data"]["internal_id"] for call in queued] == [failed]
    assert second == {"download_ids": []}
    with Session(engine) as session:
        new = session.get(Download, first["download_ids"][0])
        assert new.track_id == failed and new.requested_by == alice.id


def test_a_user_retries_only_what_they_can_see(engine, monkeypatch):
    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    _track_with(engine, "failed", requested_by=bob.id)
    client, queued = _client(engine, monkeypatch)

    assert client.post("/downloads/retry-failed", headers=_auth(alice)).json() == {"download_ids": []}
    assert queued == []


def test_an_admin_retries_everyone_s_failed_tracks(engine, monkeypatch):
    admin, bob = _user(engine, "root", role="admin"), _user(engine, "bob")
    _track_with(engine, "failed", requested_by=bob.id)
    client, _queued = _client(engine, monkeypatch)

    assert len(client.post("/downloads/retry-failed", headers=_auth(admin)).json()["download_ids"]) == 1


def test_a_guest_gets_401_and_nothing_is_queued(engine, monkeypatch):
    _track_with(engine, "failed")
    client, queued = _client(engine, monkeypatch)

    assert client.post("/downloads/retry-failed").status_code == 401
    assert queued == []
    with Session(engine) as session:
        assert len(session.exec(select(Download)).all()) == 1


def test_with_auth_disabled_everything_failed_is_retried_without_a_requester(engine, monkeypatch):
    _track_with(engine, "failed")
    client, queued = _client(engine, monkeypatch, auth_enabled=False)

    ids = client.post("/downloads/retry-failed").json()["download_ids"]

    assert len(ids) == 1 and queued[0]["user_id"] is None


def test_area_and_group_label_keys_are_present_and_stable():
    taxonomy = sc.public_taxonomy()

    assert [(a["key"], a["label_key"]) for a in taxonomy["areas"]] == [
        ("account", "settings.areas.account"),
        ("providers", "settings.areas.providers"),
        ("library", "settings.areas.library"),
        ("system", "settings.areas.system"),
    ]
    assert all(a["label"] for a in taxonomy["areas"])
    assert all(g["label_key"] == f"settings.groups.{g['key']}" for g in taxonomy["groups"])


def test_a_track_someone_else_downloaded_since_is_not_retried(engine, monkeypatch):
    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    track_id = _track_with(engine, "failed", requested_by=alice.id)
    with Session(engine) as session:
        session.add(Download(track_id=track_id, status="downloaded", requested_by=bob.id))
        session.commit()
    client, queued = _client(engine, monkeypatch)

    assert client.post("/downloads/retry-failed", headers=_auth(alice)).json() == {"download_ids": []}
    assert queued == []
