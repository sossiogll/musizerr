"""
Ogni elemento serializzato porta un ``kind`` esplicito ("track", "album",
"artist", "playlist") in ricerca, dettagli, discover e libreria: il client
non deve dedurre il tipo dai campi (una playlist ha "name" come un artista).
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from db.engine import get_session
from db.models import SYSTEM_USER_ID, TrackRecord, User, UserFavorite, UserSavedTrack
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.discover import router as discover_router
from routes.library import router as library_router
from routes.search import router as search_router
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import new_engine
from tests.test_public_urls import _ProxiedCatalog


class _SearchableCatalog(_ProxiedCatalog):
    def search_tracks(self, query, session, limit=20):
        return [self._track()]

    def search_albums(self, query, session, limit=20):
        return [self.get_album("a1", session)]

    def search_artists(self, query, session, limit=20):
        return [self.get_artist("ar1", session)]

    def search_playlists(self, query, session, limit=20):
        return [self.get_playlist("p1", session)]


@pytest.fixture(autouse=True)
def _empty_discover_cache():
    from routes import discover

    with discover._cache_lock:
        discover._cache.clear()


@pytest.fixture
def client():
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": ["proxied"]})
    with Session(engine) as session:
        session.add(User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin"))
        # track_data scritto prima che esistesse "kind": la libreria lo aggiunge.
        record = TrackRecord(title="Old", artist="A", album="B", track_data=json.dumps({"title": "Old"}))
        session.add(record)
        session.commit()
        session.add(UserSavedTrack(user_id=SYSTEM_USER_ID, track_id=record.internal_id))
        session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id=record.internal_id))
        session.commit()

    app = FastAPI()
    for router in (search_router, track_router, album_router, artist_router, playlist_router,
                   discover_router, library_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[_SearchableCatalog()])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as c:
        yield c


def _items(body):
    return body["items"] if isinstance(body, dict) and "items" in body else body


@pytest.mark.parametrize("search_type", ["track", "album", "artist", "playlist"])
def test_search_results_carry_their_kind(client, search_type):
    items = _items(client.get(f"/search?q=x&type={search_type}").json())

    assert items and {item["kind"] for item in items} == {search_type}


@pytest.mark.parametrize("path, kind", [
    ("/track/proxied/t1", "track"),
    ("/album/proxied/a1", "album"),
    ("/artist/proxied/ar1", "artist"),
    ("/playlist/proxied/p1", "playlist"),
])
def test_detail_responses_carry_their_kind(client, path, kind):
    assert client.get(path).json()["kind"] == kind


def test_nested_tracks_carry_their_kind(client):
    album = client.get("/album/proxied/a1").json()
    playlist = client.get("/playlist/proxied/p1").json()

    assert {t["kind"] for t in album["tracks"] + playlist["tracks"]} == {"track"}


def test_a_playlist_is_never_an_artist(client):
    playlist = client.get("/playlist/proxied/p1").json()
    searched = _items(client.get("/search?q=x&type=playlist").json())

    assert playlist["kind"] == "playlist"
    assert all(item["kind"] == "playlist" for item in searched)


@pytest.mark.parametrize("path, kind", [
    ("/discover/new-releases", "album"),
    ("/discover/trending/tracks", "track"),
])
def test_discover_items_carry_their_kind(client, path, kind):
    items = _items(client.get(path).json())

    assert items and {item["kind"] for item in items} == {kind}


def test_library_items_carry_their_kind_even_when_stored_before_it_existed(client):
    items = _items(client.get("/favorites").json())

    assert items and {item["kind"] for item in items} == {"track"}
