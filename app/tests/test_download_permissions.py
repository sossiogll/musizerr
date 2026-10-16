"""
Permessi reali (TestClient) delle rotte che accodano download: le quattro
``POST /download/*`` e ``POST /downloads/{track_id}/retry``. Un guest riceve
401 e non crea né accoda nulla; un utente e un admin scaricano a proprio
nome; con l'autenticazione disattivata il download resta senza richiedente.
Nessuna impostazione riapre i download anonimi.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select, text

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.download_policy import KINDS, MODE_AUTO, mode_key
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album, Artist, Playlist
from musizerr_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, TrackRecord, User
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.history import router as history_router
from services import downloads
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, make_track


class _Catalog(MetadataProvider):
    name = "catalog"
    label = "Catalog"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())
        self._track = make_track(title="Song", artist="Artist", album="Album", provider="catalog", provider_id="t1")

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_track(self, provider_id, session):
        return self._track

    def get_album(self, provider_id, session):
        return Album(title="Album", artist="Artist", provider="catalog", provider_id="a1", tracks=[self._track])

    def get_artist(self, provider_id, session):
        return Artist(name="Artist", provider="catalog", provider_id="ar1")

    def supports_discography(self):
        return True

    def get_artist_albums(self, provider_id, session, limit=50):
        return [Album(title="Album", artist="Artist", provider="catalog", provider_id="a1", album_type="album")]

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider="catalog", provider_id="p1", tracks=[self._track])


DOWNLOAD_ROUTES = ["track", "album", "artist", "playlist"]


def _make_user(engine, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-u", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _seed_track(engine) -> str:
    with Session(engine) as session:
        track = TrackRecord(title="Song", artist="Artist", album="Album")
        session.add(track)
        session.commit()
        return track.internal_id


def _downloads(engine) -> list[Download]:
    with Session(engine) as session:
        return list(session.exec(select(Download)).all())


@pytest.fixture(params=["guest", "user", "admin", "open"])
def mode(request, engine, monkeypatch):
    config = ConfigManager(engine=engine)
    # This file is about WHO may download: the approval policy
    # (``tests/test_download_requests.py``) is out of the way.
    config.update({"auth_enabled": request.param != "open", **{mode_key(kind): MODE_AUTO for kind in KINDS}})
    monkeypatch.setattr(downloads, "get_config", lambda: config)

    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    app = FastAPI()
    for router in (track_router, album_router, artist_router, playlist_router, history_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry(metadata=[_Catalog()])
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()

    headers, user_id = {}, None
    if request.param in ("user", "admin"):
        user = _make_user(engine, request.param)
        headers = {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
        user_id = user.id

    with TestClient(app) as client:
        yield {"name": request.param, "client": client, "headers": headers, "user_id": user_id, "queued": queued}


def _assert_outcome(mode, engine, resp):
    if mode["name"] == "guest":
        assert resp.status_code == 401
        assert _downloads(engine) == []
        assert mode["queued"] == []
        return
    assert resp.status_code == 200
    rows = _downloads(engine)
    assert rows and all(r.requested_by == mode["user_id"] for r in rows)
    assert mode["queued"] and all(call["user_id"] == mode["user_id"] for call in mode["queued"])


@pytest.mark.parametrize("kind", DOWNLOAD_ROUTES)
def test_download_routes(mode, engine, kind):
    resp = mode["client"].post(f"/download/{kind}", json={"provider": "catalog", "id": "x"}, headers=mode["headers"])

    _assert_outcome(mode, engine, resp)


def test_retry(mode, engine):
    track_id = _seed_track(engine)
    # A retry runs again a download that exists (``tests/test_download_requests.py``
    # covers the one that never did).
    if mode["name"] != "guest":
        with Session(engine) as session:
            session.add(Download(track_id=track_id, requested_by=mode["user_id"], status="failed"))
            session.commit()

    resp = mode["client"].post(f"/downloads/{track_id}/retry", headers=mode["headers"])

    _assert_outcome(mode, engine, resp)


def test_a_stale_stored_anonymous_downloads_value_has_no_effect(engine, monkeypatch):
    """Una riga ``allow_anonymous_downloads = true`` rimasta nel DB (prima della
    migrazione 0016) non riapre i download ai guest."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, 'allow_anonymous_downloads', 'system', NULL, :value, :now)"
            ),
            {"id": str(uuid.uuid4()), "value": json.dumps(True), "now": datetime.now(timezone.utc)},
        )
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    queued: list = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    app = FastAPI()
    app.include_router(track_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry(metadata=[_Catalog()])
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as client:
        resp = client.post("/download/track", json={"provider": "catalog", "id": "x"})

    assert resp.status_code == 401
    assert _downloads(engine) == [] and queued == []
