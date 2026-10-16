"""
URL delle risorse servite dal backend (cover, stream): il frontend raggiunge
l'API solo sotto un prefisso (``PUBLIC_API_PREFIX``, default ``/api``), quindi
ogni URL di questo tipo nelle risposte deve iniziare con quel prefisso. Un
path relativo alla root caricherebbe la pagina del frontend al posto
dell'immagine o dell'audio.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album, Artist, Playlist
from db.engine import get_session
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.discover import router as discover_router
from services import public_urls
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, make_track
from tests.loopback import host_proxy


@pytest.fixture(params=["/api", "/music/api"])
def prefix(request, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_PREFIX", request.param)
    return request.param


def test_the_builders_use_the_configured_prefix(prefix):
    assert public_urls.cover_url("prov", "c 1") == f"{prefix}/cover/provider/prov/c%201"
    assert public_urls.stored_stream_url("t1") == f"{prefix}/stream/t1"



# ---------------------------------------------------------------------------
# Scansione delle risposte: nessuna stringa che inizia con "/" fuori dal
# prefisso (cover e stream di un provider che li serve tramite l'host).
# ---------------------------------------------------------------------------


class _ProxiedCatalog(MetadataProvider):
    name = "proxied"
    label = "Proxied"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, context=None):
        super().__init__(context or StaticProviderContext(provider_name=self.name))

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _track(self):
        track = make_track(title="Song", artist="Artist", album="Album", provider=self.name, provider_id="t1")
        track.cover_url = self.context.cover_url("cover-t1")
        track.stream_url = self.context.stream_url("t1")
        return track

    def get_track(self, provider_id, session):
        return self._track()

    def get_album(self, provider_id, session):
        return Album(title="Album", artist="Artist", provider=self.name, provider_id="a1",
                     cover_url=self.context.cover_url("cover-a1"), tracks=[self._track()])

    def get_artist(self, provider_id, session):
        return Artist(name="Artist", provider=self.name, provider_id="ar1", cover_url=self.context.cover_url("cover-ar1"))

    def get_artist_albums(self, provider_id, session, limit=50):
        return [self.get_album("a1", session)]

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider=self.name, provider_id="p1",
                        cover_url=self.context.cover_url("cover-p1"), tracks=[self._track()])

    def get_new_releases(self, session, limit=20):
        return [self.get_album("a1", session)]

    def get_trending_tracks(self, session, limit=20):
        return [self._track()]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.fixture(autouse=True)
def _empty_discover_cache():
    """Discover tiene i risultati in cache per un'ora: fra un test e l'altro
    (con prefissi diversi) va svuotata."""
    from routes import discover

    with discover._cache_lock:
        discover._cache.clear()


@pytest.fixture
def client():
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": ["proxied"]})
    app = FastAPI()
    for router in (track_router, album_router, artist_router, playlist_router, discover_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[host_proxy(_ProxiedCatalog)])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", [
    "/track/proxied/t1",
    "/album/proxied/a1",
    "/artist/proxied/ar1",
    "/artist/proxied/ar1/albums",
    "/playlist/proxied/p1",
    "/discover/new-releases",
    "/discover/trending/tracks",
])
def test_no_root_relative_url_outside_the_prefix(client, prefix, path):
    resp = client.get(path)
    assert resp.status_code == 200

    rooted = [s for s in _strings(resp.json()) if s.startswith("/")]
    assert rooted, "la risposta dovrebbe contenere almeno una cover servita dall'host"
    outside = [s for s in rooted if not s.startswith(prefix + "/")]
    assert outside == []


def test_the_urls_a_provider_builds_for_its_covers_and_streams_carry_the_prefix(prefix):
    from musizerr_sdk.auth import AuthSession

    track = host_proxy(_ProxiedCatalog).get_track("t1", AuthSession.anonymous())

    assert track.cover_url == f"{prefix}/cover/provider/proxied/cover-t1"
    assert track.stream_url == f"{prefix}/stream/provider/proxied/t1"
