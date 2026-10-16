"""
The first full stream after a restart waited ~30 s for the storage's
timeout ("unreachable: timed out") before playing the sync provider's copy:
the storage got the whole connect timeout, then a second try (``exists``,
to tell "lost" from "unreachable") got it again. Now every step before the
storage's first byte waits at most ``stream_start_timeout``, a timeout is
already the answer (no second try), and a storage that did not answer rests
(``storage_stream_cooldown_seconds``): meanwhile a track with a sync copy
plays that copy at once.

Time is virtual: the fake storage spends it on a fake clock, as a real one
would spend it waiting on a socket with the timeout it was given.
"""
from __future__ import annotations


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.identifiers import sync_id_type
from musizerr_sdk.interfaces import StorageStreamResult
from musizerr_sdk.context import StaticProviderContext
from db.engine import get_session
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord
from routes import deps
from routes import stream as stream_route
from routes.errors import install_error_handlers
from services.config_manager import ConfigManager
from services.provider_cooldown import RedisProviderCooldown
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRedis, FakeRegistry, FakeStorageProvider, FakeSyncProvider
from tests.loopback import as_proxy
from services.provider_proxy import ProviderCallFailed

START_TIMEOUT = 5
REST_SECONDS = 120
PATH = "A/B/T.ogg"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SlowStorage(FakeStorageProvider):
    """A storage that takes ``delay`` seconds to answer: a stream waits for
    it up to the stream start timeout of its context, then times out."""

    def __init__(self, clock: _Clock, delay: float) -> None:
        super().__init__("shelf")
        self.context = StaticProviderContext(stream_start_timeout=START_TIMEOUT)
        self.clock = clock
        self.delay = delay
        self.files = {PATH: b"stored-audio"}
        self.streams = 0
        self.exists_calls = 0

    def stream(self, relative_path, workspace, session):
        self.streams += 1
        waited = min(self.delay, self.context.stream_start_timeout)
        self.clock.now += waited
        if self.delay > self.context.stream_start_timeout:
            raise TimeoutError("timed out")
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)
        return StorageStreamResult(chunks=iter([self.files[relative_path]]), media_type="audio/ogg")

    def exists(self, relative_path, session):
        self.exists_calls += 1
        self.clock.now += self.delay  # a second connection, as slow as the first
        return relative_path in self.files


def _sync_audio(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"sync-audio", headers={"content-type": "audio/ogg"})


@pytest.fixture
def env(monkeypatch):
    engine = new_engine()
    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        track_id = record.internal_id
        session.add(Download(track_id=track_id, status="downloaded", file_path=PATH))
        session.add(LibraryEntry(track_id=track_id, file_path=PATH, file_format="ogg", storage_provider="shelf"))
        session.commit()

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "storage_provider": "shelf"})
    clock = _Clock()
    redis = FakeRedis(clock=clock)
    storage = _SlowStorage(clock, delay=30)
    sync = FakeSyncProvider()
    monkeypatch.setattr(stream_route, "_upstream_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(_sync_audio)))

    app = FastAPI()
    app.include_router(stream_route.router)
    install_error_handlers(app)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry(storage=[storage], syncs=[sync])
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    app.dependency_overrides[deps.get_stream_cooldown] = lambda: RedisProviderCooldown(
        errors=1, seconds=REST_SECONDS, client=redis, scope="storage-stream")

    def add_sync_copy():
        with Session(engine) as session:
            session.add(TrackIdentifierRecord(track_id=track_id, id_type=sync_id_type(sync.name), value="s1"))
            session.commit()

    return TestClient(app), track_id, storage, clock, add_sync_copy, engine


def _timed(client, clock, url):
    start = clock.now
    response = client.get(url)
    return response, clock.now - start


def test_a_storage_that_does_not_answer_falls_back_to_the_sync_copy_within_the_start_timeout(env):
    client, track_id, storage, clock, add_sync_copy, _ = env
    add_sync_copy()

    response, waited = _timed(client, clock, f"/stream/{track_id}")

    assert response.status_code == 200 and response.content == b"sync-audio"
    assert waited <= START_TIMEOUT
    assert storage.exists_calls == 0  # a timeout is the answer: no second connection


def test_while_the_storage_rests_the_sync_copy_plays_at_once(env):
    client, track_id, storage, clock, add_sync_copy, _ = env
    add_sync_copy()
    client.get(f"/stream/{track_id}")

    response, waited = _timed(client, clock, f"/stream/{track_id}")

    assert response.content == b"sync-audio"
    assert waited == 0 and storage.streams == 1


def test_after_the_rest_the_storage_is_asked_again(env):
    client, track_id, storage, clock, add_sync_copy, _ = env
    add_sync_copy()
    client.get(f"/stream/{track_id}")
    clock.now += REST_SECONDS + 1
    storage.delay = 0  # back

    response = client.get(f"/stream/{track_id}")

    assert response.content == b"stored-audio" and storage.streams == 2


def test_a_resting_storage_is_still_tried_for_a_track_without_a_sync_copy(env):
    client, track_id, storage, clock, add_sync_copy, _ = env
    client.get(f"/stream/{track_id}")  # unreachable, no copy: 503
    storage.delay = 0

    response = client.get(f"/stream/{track_id}")

    assert response.content == b"stored-audio" and storage.streams == 2


def test_without_a_sync_copy_an_unreachable_storage_is_a_quick_503(env):
    client, track_id, storage, clock, _add_sync_copy, _ = env

    response, waited = _timed(client, clock, f"/stream/{track_id}")

    assert response.status_code == 503 and waited <= START_TIMEOUT


def test_a_file_the_storage_lost_does_not_make_it_rest(env):
    client, track_id, storage, clock, add_sync_copy, _ = env
    add_sync_copy()
    storage.delay = 0
    storage.files = {}

    assert client.get(f"/stream/{track_id}").content == b"sync-audio"  # lost: the copy
    storage.files = {PATH: b"stored-audio"}
    assert client.get(f"/stream/{track_id}").content == b"stored-audio"  # not resting


def test_a_file_known_missing_is_never_asked_to_the_storage(env):
    from datetime import datetime, timezone

    from sqlmodel import select

    client, track_id, storage, clock, add_sync_copy, engine = env
    add_sync_copy()
    with Session(engine) as session:
        entry = session.exec(select(LibraryEntry)).one()
        entry.missing_since = datetime.now(timezone.utc)
        session.add(entry)
        session.commit()

    response = client.get(f"/stream/{track_id}")

    assert response.content == b"sync-audio" and storage.streams == 0


def test_playback_is_full_only_with_a_source_stream_can_play(env):
    from datetime import datetime, timezone

    from sqlmodel import select

    client, track_id, _storage, _clock, add_sync_copy, engine = env
    with Session(engine) as session:
        session.add(TrackIdentifierRecord(track_id=track_id, id_type="pa_id", value="p1"))
        session.commit()
    assert client.get("/playback/pa/p1").json()["type"] == "full"

    with Session(engine) as session:
        entry = session.exec(select(LibraryEntry)).one()
        entry.missing_since = datetime.now(timezone.utc)
        session.add(entry)
        session.commit()
    assert client.get("/playback/pa/p1").json()["type"] != "full"

    add_sync_copy()
    assert client.get("/playback/pa/p1").json()["type"] == "full"


def test_an_unreachable_storage_is_told_apart_from_a_lost_file_without_a_second_connection():
    # What the host is told of the failures: the runner classified the timeout
    # by its type; the error of a library that said nothing carries no kind.
    exc = ProviderCallFailed("timeout", "timed out", exception_type="TimeoutError")
    lost = ProviderCallFailed(None, "550 No such file", exception_type="error_perm")

    class _Storage(FakeStorageProvider):
        def exists(self, path, session):
            raise AssertionError("not asked")

    assert stream_route._storage_failure_kind(as_proxy(_Storage()), PATH, None, exc) == "unreachable"
    assert stream_route._storage_failure_kind(as_proxy(FakeStorageProvider()), PATH, None, lost) == "not_found"
