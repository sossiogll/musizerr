"""Shared by the playlist tests: a metadata provider with playlists, a sync
provider that keeps them, and the API built around them. Fakes only, no real
provider."""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Playlist, Track, TracksUnavailable
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import RemotePlaylistGone
from core.download_policy import KINDS, MODE_AUTO, mode_key
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import SYSTEM_USER_ID, User
from routes import deps
from routes.errors import install_error_handlers
from routes.library import router as library_router
from routes.playlists import router as playlists_router
from services import downloads
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track
from tests.database import seed_system_user


class Source(MetadataProvider):
    """A catalog with one playlist whose tracks, name and snapshot the test
    changes; counts what the host asks."""

    name = "catalog"
    label = "Catalog"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())
        self.tracks = [make_track(title=f"Song {n}", provider="catalog", provider_id=f"t{n}") for n in (1, 2, 3)]
        self.snapshot: str | None = "v1"
        self.unavailable: TracksUnavailable | None = None
        self.exists = True
        self.fail_ids: set[str] = set()  # playlists whose provider call raises
        self.snapshot_calls = 0
        self.playlist_calls = 0

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_track(self, provider_id, session):
        return next((t for t in self.tracks if t.provider_id == provider_id), None) or make_track(
            title=f"Other {provider_id}", provider="catalog", provider_id=provider_id)

    def get_playlist(self, provider_id, session):
        self.playlist_calls += 1
        self._maybe_fail(provider_id)
        if not self.exists:
            return None
        return Playlist(
            name="Mix", provider="catalog", provider_id=provider_id, description="A mix",
            tracks=[] if self.unavailable else list(self.tracks), snapshot_id=self.snapshot,
            track_count=len(self.tracks), tracks_unavailable=self.unavailable,
        )

    def get_playlist_snapshot(self, provider_id, session):
        self.snapshot_calls += 1
        self._maybe_fail(provider_id)
        return self.snapshot

    def _maybe_fail(self, provider_id):
        if provider_id in self.fail_ids:
            raise ConnectionError("connection refused")

    def restricts_playlist_tracks(self):
        return True


class PlaylistSyncProvider(FakeSyncProvider):
    """A sync provider that keeps playlists: remembers what it was sent."""

    auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL)

    def __init__(self, name: str = "fakesync", **kwargs):
        super().__init__(name, **kwargs)
        self.playlists: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail = False
        self._next = 0

    def supports_playlists(self) -> bool:
        return True

    def create_playlist(self, name, sync_ids, session):
        self.calls.append(("create", name, list(sync_ids)))
        if self.fail:
            raise ConnectionError("connection refused")
        self._next += 1
        remote_id = f"remote-{self._next}"
        self.playlists[remote_id] = {"name": name, "ids": list(sync_ids), "account": session.extra.get("username")}
        return remote_id

    def update_playlist(self, remote_id, name, sync_ids, session):
        self.calls.append(("update", remote_id, name, list(sync_ids)))
        if self.fail:
            raise ConnectionError("connection refused")
        if remote_id not in self.playlists:
            raise RemotePlaylistGone(remote_id)
        self.playlists[remote_id].update(name=name, ids=list(sync_ids))

    def delete_playlist(self, remote_id, session):
        self.calls.append(("delete", remote_id))
        if remote_id not in self.playlists:
            raise RemotePlaylistGone(remote_id)
        del self.playlists[remote_id]


def make_user(engine, name: str, role: str = "user") -> User:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@dataclass
class Api:
    client: TestClient
    config: ConfigManager
    source: Source
    registry: FakeRegistry
    queued: list = field(default_factory=list)

    def as_(self, user: User | None) -> dict:
        return {} if user is None else {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    def call(self, user, method: str, url: str, **kwargs):
        return self.client.request(method, url, headers=self.as_(user), **kwargs)


def build_api(engine, monkeypatch, *, auth: bool = True, syncs=()) -> Api:
    """The playlist routes (and the library ones, for the favorites) over the
    fake catalog, with the downloads queue captured and every request kind
    approved at once; a test that wants approval sets the mode."""
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": auth, **{mode_key(kind): MODE_AUTO for kind in KINDS}})
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(playlists_router)
    app.include_router(library_router)

    def _session():
        with Session(engine) as s:
            yield s

    source = Source()
    registry = FakeRegistry(metadata=[source], syncs=list(syncs))
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    if not auth:
        seed_system_user(engine)
    return Api(TestClient(app), config, source, registry, queued)


__all__ = ["Api", "PlaylistSyncProvider", "Source", "build_api", "make_user", "make_track", "Track", "SYSTEM_USER_ID"]
