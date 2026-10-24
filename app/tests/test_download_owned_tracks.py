"""
A track already in the library never gets a new download, from any entry
point, unless an admin forces it.

Real case (29 Sep): "Taxi sulla Luna", in the library since 7 Sep, requested
again from a row of a second metadata provider, was dispatched to the
album-level downloader; "EVERYDAY" was downloaded a second time (two files in
the library); "Mil pasos" was requested while in the library. The request
path only checked for an OPEN request, never for the library.

The library here is every source ``db.library_store`` knows: a landed
download, a library entry (download or scan), a sync provider's id.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import download_states as ds
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.identifiers import sync_id_type
from musizeer_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Album, Artist, Playlist, Track
from musizeer_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord, User
from db.track_store import find_or_create_track
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.history import router as history_router
from services import downloads
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider

ISRC_VALUE = "ITUM72300873"


def _row(provider: str, provider_id: str, *, title: str = "Taxi sulla Luna", isrc: str | None = ISRC_VALUE) -> Track:
    """A track as a metadata provider's list returns it."""
    ids = [Identifier(type=PROVIDER_ID, value=provider_id, provider=provider)]
    if isrc:
        ids.append(Identifier(type=ISRC, value=isrc))
    return Track(title=title, artist="Emma", album="Taxi sulla Luna", duration_ms=180_000,
                 provider=provider, provider_id=provider_id, external_ids=ids)


class _Catalog(MetadataProvider):
    """A metadata provider whose every read returns the same row."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str, row: Track) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self._row = row

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_track(self, provider_id, session):
        return Track.from_dict(self._row.to_dict())

    def get_album(self, provider_id, session):
        return Album(title="Taxi sulla Luna", artist="Emma", provider=self.name, provider_id="a1",
                     tracks=[self.get_track(provider_id, session)])

    def get_artist(self, provider_id, session):
        return Artist(name="Emma", provider=self.name, provider_id="ar1")

    def supports_discography(self):
        return True

    def get_artist_albums(self, provider_id, session, limit=50):
        return [Album(title="Taxi sulla Luna", artist="Emma", provider=self.name, provider_id="a1", album_type="album")]

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider=self.name, provider_id="p1", tracks=[self.get_track(provider_id, session)])


@pytest.fixture
def queued(engine, monkeypatch):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    calls: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: calls.append(kw))
    return calls


def _user(engine, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-u", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _client(engine, *, sync=None) -> TestClient:
    app = FastAPI()
    for router in (track_router, album_router, artist_router, playlist_router, history_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    config = ConfigManager(engine=engine)
    registry = FakeRegistry(
        metadata=[_Catalog("primary", _row("primary", "p-1")), _Catalog("secondary", _row("secondary", "s-1"))],
        syncs=[sync] if sync else [],
    )
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    return TestClient(app)


def _headers(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


def _own(engine, source: str) -> str:
    """The track, as the primary provider knows it, in the library through ``source``."""
    with Session(engine) as session:
        record = find_or_create_track(_row("primary", "p-1"), session)
        if source == "download":
            session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path="Emma/Taxi.ogg"))
        elif source == "entry":
            session.add(LibraryEntry(track_id=record.internal_id, file_path="Emma/Taxi.ogg", file_format="ogg",
                                     source="scan", storage_provider="disk"))
        elif source == "sync":
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=sync_id_type("fakesync"), value="nd-1"))
        session.commit()
        return record.internal_id


def _new_downloads(engine, track_id: str) -> list[Download]:
    with Session(engine) as session:
        return [d for d in session.exec(select(Download).where(Download.track_id == track_id)).all()
                if d.status != ds.DOWNLOADED]


SOURCES = ["download", "entry", "sync"]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("row_provider", ["primary", "secondary"])
def test_a_track_in_the_library_is_not_downloaded_again_from_any_provider_row(engine, queued, source, row_provider):
    track_id = _own(engine, source)
    user = _user(engine, "user")

    with _client(engine, sync=FakeSyncProvider("fakesync")) as client:
        resp = client.post("/download/track", json={"provider": row_provider, "id": "x"}, headers=_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "already_in_library"
    assert body["download_id"] is None
    assert body["library_id"] == track_id
    assert _new_downloads(engine, track_id) == []
    assert queued == []


@pytest.mark.parametrize("kind", ["album", "playlist", "artist"])
def test_a_collection_download_skips_the_tracks_in_the_library(engine, queued, kind):
    track_id = _own(engine, "entry")
    user = _user(engine, "user")

    with _client(engine) as client:
        resp = client.post(f"/download/{kind}", json={"provider": "secondary", "id": "x"}, headers=_headers(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["download_ids"] == []
    assert _new_downloads(engine, track_id) == []
    assert queued == []


def test_a_retry_of_a_track_in_the_library_creates_no_download(engine, queued):
    track_id = _own(engine, "entry")
    user = _user(engine, "user")
    with Session(engine) as session:
        session.add(Download(track_id=track_id, status=ds.FAILED, requested_by=user.id))
        session.commit()

    with _client(engine) as client:
        resp = client.post(f"/downloads/{track_id}/retry", headers=_headers(user))
        failed = client.post("/downloads/retry-failed", headers=_headers(_user(engine, "admin")))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "already_in_library", "download_id": None, "library_id": track_id}
    assert failed.json() == {"download_ids": []}
    assert [d.status for d in _new_downloads(engine, track_id)] == [ds.FAILED]
    assert queued == []


def test_an_admin_can_force_a_new_download_of_a_track_in_the_library(engine, queued):
    track_id = _own(engine, "download")
    admin = _user(engine, "admin")

    with _client(engine) as client:
        resp = client.post("/download/track", json={"provider": "secondary", "id": "x", "force": True},
                           headers=_headers(admin))
        retry = client.post(f"/downloads/{track_id}/retry?force=true", headers=_headers(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["download_id"]
    # The retry joins the forced request already open instead of a third copy.
    assert retry.json() == {"status": "attached", "download_id": body["download_id"], "library_id": track_id}
    assert [d.status for d in _new_downloads(engine, track_id)] == [ds.PENDING]
    assert len(queued) == 1


def test_force_is_admin_only(engine, queued):
    track_id = _own(engine, "download")
    user = _user(engine, "user")

    with _client(engine) as client:
        resp = client.post("/download/track", json={"provider": "secondary", "id": "x", "force": True},
                           headers=_headers(user))
        retry = client.post(f"/downloads/{track_id}/retry?force=true", headers=_headers(user))

    assert resp.status_code == 403
    assert retry.status_code == 403
    assert _new_downloads(engine, track_id) == [] and queued == []


def test_a_track_not_in_the_library_is_queued_and_says_so(engine, queued):
    user = _user(engine, "user")

    with _client(engine) as client:
        resp = client.post("/download/track", json={"provider": "secondary", "id": "x"}, headers=_headers(user))
        again = client.post("/download/track", json={"provider": "primary", "id": "x"}, headers=_headers(user))

    body = resp.json()
    assert body["status"] == "queued" and body["download_id"] and body["library_id"]
    assert again.json()["status"] == "attached"
    assert again.json()["download_id"] == body["download_id"]
    assert len(queued) == 1


def test_the_library_sync_job_does_not_queue_a_track_in_the_library(engine, queued):
    """``enqueue_track`` is the one gate every bulk path goes through."""
    track_id = _own(engine, "sync")
    with Session(engine) as session:
        result = downloads.enqueue_track(
            _row("secondary", "s-1"), session, registry=FakeRegistry(syncs=[FakeSyncProvider("fakesync")]),
        )

    assert result.status == downloads.ALREADY_IN_LIBRARY
    assert result.library_id == track_id and result.download_id is None
    assert _new_downloads(engine, track_id) == [] and queued == []
