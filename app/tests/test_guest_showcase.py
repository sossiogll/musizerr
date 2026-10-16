"""
Profilo guest "showcase" (autenticazione attiva, nessun token): può vedere
discover, i dettagli di track/album/artist/playlist e i lyrics, e ascoltare
le preview; non può cercare, scaricare, ritentare un download né ascoltare
l'audio completo.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import LyricsProvider
from musizerr_sdk.models import Lyrics
from musizerr_sdk.context import StaticProviderContext
from db.engine import get_session
from db.models import TrackRecord
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.discover import router as discover_router
from routes.history import router as history_router
from routes.search import router as search_router
from routes.stream import router as stream_router
from services import downloads
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine
from tests.test_download_permissions import _Catalog

PREVIEW = "https://example.invalid/preview.mp3"


class _ShowcaseCatalog(_Catalog):
    def __init__(self):
        super().__init__()
        self._track.preview_url = PREVIEW

    def get_new_releases(self, session, limit=20):
        return [self.get_album("a1", session)]

    def get_trending_tracks(self, session, limit=20):
        return [self._track]


class _Lyrics(LyricsProvider):
    name = "lyricsfake"
    label = "Lyrics"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        return Lyrics(plain="la la la", source=self.name)


@pytest.fixture
def guest(monkeypatch):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "metadata_provider_priority": ["catalog"]})
    queued: list = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    app = FastAPI()
    for router in (
        discover_router, track_router, album_router, artist_router, playlist_router,
        search_router, history_router, stream_router,
    ):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[_ShowcaseCatalog()], lyrics=[_Lyrics()])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as client:
        yield client, engine, queued


@pytest.mark.parametrize("path", [
    "/discover/new-releases",
    "/discover/trending/tracks",
    "/track/catalog/t1",
    "/track/catalog/t1/lyrics",
    "/album/catalog/a1",
    "/artist/catalog/ar1",
    "/playlist/catalog/p1",
])
def test_a_guest_can_browse(guest, path):
    client, _engine, _queued = guest

    assert client.get(path).status_code == 200


def test_track_details_keep_the_preview_for_a_guest(guest):
    client, _engine, _queued = guest

    assert client.get("/track/catalog/t1").json()["preview_url"] == PREVIEW


def test_a_guest_cannot_search(guest):
    client, _engine, _queued = guest

    assert client.get("/search?q=song").status_code == 401


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
def test_a_guest_cannot_download(guest, kind):
    client, _engine, queued = guest

    assert client.post(f"/download/{kind}", json={"provider": "catalog", "id": "x"}).status_code == 401
    assert queued == []


def test_a_guest_cannot_retry(guest):
    client, engine, queued = guest
    with Session(engine) as session:
        track = TrackRecord(title="T", artist="A", album="B")
        session.add(track)
        session.commit()
        track_id = track.internal_id

    assert client.post(f"/downloads/{track_id}/retry").status_code == 401
    assert queued == []


def test_a_guest_cannot_stream_the_full_track(guest):
    client, _engine, _queued = guest

    assert client.get("/stream/any-track").status_code == 401
    assert client.get("/stream/provider/catalog/t1").status_code == 401
