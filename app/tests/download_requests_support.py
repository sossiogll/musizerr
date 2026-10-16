"""
Shared scaffolding of the download-approval tests: a catalog whose item ids
say how many tracks they hold, a download provider that takes whole albums, an
application with every route of the policy wired to a test database, and
helpers to count what a request left behind (downloads, favorite intents,
requests). Not a test module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import download_policy as dp
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.errors import DownloadDelegated
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album, Artist, Playlist
from musizerr_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, DownloadRequest, PendingLike, User
from db.track_store import find_or_create_track
from routes import deps
from routes.admin import router as admin_router
from routes.admin_download_requests import router as admin_requests_router
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.download_requests import router as requests_router
from routes.errors import install_error_handlers
from routes.history import router as history_router
from routes.users_auth import router as account_router
from services import downloads
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeDownloadProvider, FakeRegistry, make_album, make_track
from tests.database import seed_system_user
from tests.test_openapi_responses import assert_conforms


def count_of(item_id: str) -> int:
    """Catalog ids end with the number of tracks the item holds: ``album-x-12``."""
    return int(item_id.rsplit("-", 1)[1])


def tracks_of(tag: str, count: int):
    """``count`` distinct tracks (each with an ISRC of its own) named after ``tag``."""
    return [
        make_track(f"{tag} song {i}", "Artist", tag, provider="catalog", provider_id=f"{tag}-{i}", isrc=f"ISRC-{tag}-{i}")
        for i in range(count)
    ]


class Catalog(MetadataProvider):
    name = "catalog"
    label = "Catalog"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_track(self, provider_id, session):
        return tracks_of(provider_id, 1)[0]

    def get_album(self, provider_id, session):
        album = make_album(provider_id, "Artist", provider="catalog", provider_id=provider_id)
        album.tracks = tracks_of(provider_id, count_of(provider_id))
        return album

    def get_playlist(self, provider_id, session):
        return Playlist(name=provider_id, provider="catalog", provider_id=provider_id, tracks=tracks_of(provider_id, count_of(provider_id)))

    def get_artist(self, provider_id, session):
        return Artist(name=provider_id, provider="catalog", provider_id=provider_id)

    def supports_discography(self):
        return True

    def get_artist_albums(self, provider_id, session, limit=50):
        """One album holding the artist's tracks: it carries the artist's id, so it holds as many tracks."""
        return [make_album(provider_id, provider_id, provider="catalog", provider_id=provider_id)]


class Delegator(FakeDownloadProvider):
    """A download provider that takes a whole album (``dispatch_album``) and
    records the albums it was handed."""

    def __init__(self, name: str = "delegator", *, delegates: bool = True):
        super().__init__(name)
        self.delegates = delegates
        self.dispatched: list[str] = []

    def dispatch_album(self, album, session):
        self.dispatched.append(album.title)
        if self.delegates:
            raise DownloadDelegated(self.name, details=[{"album": album.title, "status": "queued"}])


class Clock:
    """A driven clock for the policy's decisions and quota window."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def make_user(engine, name: str, role: str = "user") -> User:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


class Harness:
    """The whole HTTP surface of the policy over a test database. ``who`` is
    ``guest``, one of the users made here (``user``, ``other``, ``admin``,
    ``admin2``) or ``open`` (authentication off: the built-in administrator)."""

    def __init__(self, engine, monkeypatch, *, auth: bool = True, delegator: Delegator | None = None, metadata=None):
        self.engine = engine
        self.config = ConfigManager(engine=engine)
        self.config.update({"auth_enabled": auth})
        self.delegator = delegator
        self.registry = FakeRegistry(metadata=metadata or [Catalog()], downloads=[delegator] if delegator else [])
        self.queued: list[dict] = []
        monkeypatch.setattr(downloads, "get_config", lambda: self.config)
        monkeypatch.setattr(downloads, "queue_download", lambda **kw: self.queued.append(kw))

        app = FastAPI()
        install_error_handlers(app)
        for router in (track_router, album_router, playlist_router, artist_router, history_router, requests_router):
            app.include_router(router)
        app.include_router(admin_router, prefix="/admin")
        app.include_router(admin_requests_router, prefix="/admin")
        app.include_router(account_router, prefix="/auth/account")

        def _session():
            with Session(engine) as session:
                yield session

        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[deps.get_config] = lambda: self.config
        app.dependency_overrides[deps.get_registry] = lambda: self.registry
        app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
        self.client = TestClient(app)

        self.users: dict[str, User] = {}
        if auth:
            for name, role in (("user", "user"), ("other", "user"), ("admin", "admin"), ("admin2", "admin")):
                self.users[name] = make_user(engine, name, role)
        else:
            seed_system_user(engine)

    def headers(self, who: str) -> dict:
        if who not in self.users:
            return {}
        user = self.users[who]
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    def call(self, who: str, method: str, url: str, *, doc: str | None = None, **kwargs):
        """The request as ``who``; with ``doc`` (the documented path) the answer must also
        conform to the OpenAPI spec."""
        resp = self.client.request(method, url, headers=self.headers(who), **kwargs)
        if doc is not None:
            assert_conforms(resp, method, doc)
        return resp

    def decide(self, who: str, action: str, request_id: str, **body):
        """One request through the batch route ``/admin/download-requests/{action}`` (``approve`` or
        ``deny``): its entry of ``results`` — ``id``, ``outcome``, ``request``."""
        url = f"/admin/download-requests/{action}"
        resp = self.call(who, "POST", url, json={"ids": [request_id], **body}, doc=url)
        assert resp.status_code == 200, resp.text
        (result,) = resp.json()["results"]
        return result

    def id(self, who: str) -> str:
        return self.users[who].id

    def policy(self, **settings) -> None:
        """General policy settings by short name: ``album_mode="approval"``,
        ``album_threshold=5``, ``quota=10``, ``quota_days=7``; ``all_modes="auto"``."""
        updates = {}
        for key, value in settings.items():
            if key == "all_modes":
                updates.update({dp.mode_key(kind): value for kind in dp.KINDS})
            elif key == "quota":
                updates[dp.QUOTA_TRACKS_KEY] = value
            elif key == "quota_days":
                updates[dp.QUOTA_DAYS_KEY] = value
            elif key.endswith("_mode"):
                updates[dp.mode_key(key[: -len("_mode")])] = value
            elif key.endswith("_threshold"):
                updates[dp.threshold_key(key[: -len("_threshold")])] = value
            else:
                raise KeyError(key)
        self.config.update(updates)


def downloads_of(engine) -> list[Download]:
    with Session(engine) as session:
        return list(session.exec(select(Download)).all())


def requests_of(engine) -> list[DownloadRequest]:
    with Session(engine) as session:
        return list(session.exec(select(DownloadRequest).order_by(DownloadRequest.created_at, DownloadRequest.id)).all())


def intents_of(engine) -> list[PendingLike]:
    with Session(engine) as session:
        return list(session.exec(select(PendingLike)).all())


def seed_in_library(engine, tracks) -> None:
    """The tracks are in the library (a download landed)."""
    with Session(engine) as session:
        for track in tracks:
            record = find_or_create_track(track, session)
            session.add(Download(track_id=record.internal_id, status="downloaded"))
        session.commit()


def seed_open_download(engine, tracks, *, requested_by: str | None = None) -> None:
    """The tracks have a download already on its way."""
    with Session(engine) as session:
        for track in tracks:
            record = find_or_create_track(track, session)
            session.add(Download(track_id=record.internal_id, status="pending", requested_by=requested_by))
        session.commit()
