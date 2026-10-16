"""
The cover/stream proxy over REAL HTTP: uvicorn serves the app from a thread,
a raw-socket upstream plays the misbehaving origin, and a real HTTP client
reads the answer.

What broke in production: an upstream answered a missing cover with 200,
``Content-Type: application/json``, ``Content-Encoding: gzip`` and the
compressed ``Content-Length``; the proxy copied that length but streamed the
decompressed (longer) body, so uvicorn aborted after the headers with
``RuntimeError: Response content longer than Content-Length`` and the client
got a truncated 200 (curl 18). Every case here must either complete with a
body that matches its headers, or fail with a clean HTTP error before the
headers — never a RuntimeError on the server.
"""
from __future__ import annotations

import gzip
import json
import logging
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.identifiers import sync_id_type
from db.engine import get_session
from db.models import Download, TrackIdentifierRecord, TrackRecord
from routes import deps, stream
from routes.errors import install_error_handlers
from routes.stream import router as stream_router
from services.config_manager import ConfigManager
from tests.database import new_engine
from tests.conftest import (
    FakeAuthManager,
    FakeMetadataProvider,
    FakeRegistry,
    FakeStorageProvider,
    FakeSyncProvider,
    make_track,
)

IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
AUDIO = b"ID3" + bytes(range(256)) * 200
MISSING_ARTWORK = json.dumps({
    "subsonic-response": {
        "status": "failed", "version": "1.16.1", "type": "upstream", "serverVersion": "0",
        "openSubsonic": True, "error": {"code": 70, "message": "Artwork not found"},
    },
}).encode()


# ---------------------------------------------------------------------------
# Upstream: a raw socket server, so each answer is exactly the bytes on the wire
# ---------------------------------------------------------------------------


def _head(status: str, headers: dict[str, str]) -> bytes:
    lines = [f"HTTP/1.1 {status}", *(f"{k}: {v}" for k, v in headers.items()), "Connection: close", "", ""]
    return "\r\n".join(lines).encode()


def _chunked(body: bytes, size: int = 1000) -> bytes:
    out = b""
    for i in range(0, len(body), size):
        part = body[i:i + size]
        out += f"{len(part):x}\r\n".encode() + part + b"\r\n"
    return out + b"0\r\n\r\n"


class _Upstream(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.requests: list[tuple[str, dict[str, str]]] = []
        # The server can still be answering the client after the proxied
        # response went out; tests that assert on requests wait for it.
        self.hits = threading.Semaphore(0)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _UpstreamHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline().decode().strip()
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline().decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        path = request_line.split(" ")[1]
        self.server.requests.append((path, headers))
        name = path.rsplit("/", 1)[-1]
        self.wfile.write(_ANSWERS[name](headers))
        self.wfile.flush()
        self.server.hits.release()


def _gzip_json(_headers) -> bytes:
    # The production answer: gzip-encoded body, compressed Content-Length —
    # sent whatever Accept-Encoding says (some origins ignore it).
    body = gzip.compress(MISSING_ARTWORK)
    assert len(body) < len(MISSING_ARTWORK)
    return _head("200 OK", {
        "Content-Type": "application/json", "Content-Encoding": "gzip", "Content-Length": str(len(body)),
    }) + body


def _gzip_image(_headers) -> bytes:
    body = gzip.compress(IMAGE)
    return _head("200 OK", {
        "Content-Type": "image/png", "Content-Encoding": "gzip", "Content-Length": str(len(body)),
    }) + body


def _gzip_if_accepted(headers) -> bytes:
    # A well-behaved origin: compresses only when the client allows it.
    if "gzip" in headers.get("accept-encoding", ""):
        return _gzip_image(headers)
    return _head("200 OK", {"Content-Type": "image/png", "Content-Length": str(len(IMAGE))}) + IMAGE


def _chunked_image(_headers) -> bytes:
    return _head("200 OK", {"Content-Type": "image/jpeg", "Transfer-Encoding": "chunked"}) + _chunked(IMAGE)


def _json_error(_headers) -> bytes:
    return _head("200 OK", {
        "Content-Type": "application/json", "Content-Length": str(len(MISSING_ARTWORK)),
    }) + MISSING_ARTWORK


def _premature(_headers) -> bytes:
    # Declares the whole file, sends a tenth of it and hangs up.
    return _head("200 OK", {"Content-Type": "audio/mpeg", "Content-Length": str(len(AUDIO))}) + AUDIO[: len(AUDIO) // 10]


def _premature_chunked(_headers) -> bytes:
    return _head("200 OK", {"Content-Type": "audio/mpeg", "Transfer-Encoding": "chunked"}) + _chunked(AUDIO)[:3000]


def _audio(_headers) -> bytes:
    return _head("200 OK", {"Content-Type": "audio/flac", "Content-Length": str(len(AUDIO))}) + AUDIO


def _missing(_headers) -> bytes:
    return _head("404 Not Found", {"Content-Type": "text/plain", "Content-Length": "9"}) + b"not found"


_ANSWERS = {
    "gzip-json": _gzip_json,
    "gzip-image": _gzip_image,
    "negotiated-image": _gzip_if_accepted,
    "chunked-image": _chunked_image,
    "json-error": _json_error,
    "premature": _premature,
    "premature-chunked": _premature_chunked,
    "audio": _audio,
    "missing": _missing,
}


# ---------------------------------------------------------------------------
# App under uvicorn, in a thread
# ---------------------------------------------------------------------------


class _Covers(FakeMetadataProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, upstream: _Upstream) -> None:
        super().__init__("covers")
        self._upstream = upstream

    def get_cover_url(self, cover_id, session):
        return f"{self._upstream.url}/cover/{cover_id}"


class _Sync(FakeSyncProvider):
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, upstream: _Upstream) -> None:
        super().__init__("syncfake")
        self._upstream = upstream

    def get_stream_url(self, sync_id, session):
        return f"{self._upstream.url}/stream/{sync_id}"


class _Store(FakeStorageProvider):
    """A storage whose ``stream`` fails the way a remote one does (e.g. an
    FTP ``550 No such file``); ``exists`` says whether the file is gone or
    the storage is down."""

    def __init__(self) -> None:
        super().__init__("store")
        self.down = False

    def exists(self, relative_path, session):
        if self.down:
            raise ConnectionRefusedError("storage down")
        return relative_path in self.files

    def stream(self, relative_path, workspace, session):
        if self.down:
            raise ConnectionRefusedError("storage down")
        raise OSError(f"550 {relative_path}: No such file or directory")


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def runtime_errors(self) -> list[logging.LogRecord]:
        return [
            r for r in self.records
            if (r.exc_info and isinstance(r.exc_info[1], RuntimeError)) or "Content-Length" in r.getMessage()
        ]


@dataclass
class _Served:
    base: str
    records: _Records
    upstream: _Upstream
    engine: object
    store: _Store
    server: uvicorn.Server

    def __iter__(self):
        return iter((self.base, self.records, self.upstream))


@pytest.fixture
def upstream():
    server = _Upstream()
    # A short poll: shutdown() waits up to one interval (0.5 s by default).
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module", params=["httptools", "h11"])
def _uvicorn(request):
    """The app on a real socket, with the HTTP implementation production
    uses (httptools, uvicorn[standard]) and the pure-Python one. Started
    once per implementation (uvicorn's shutdown alone takes ~0.2 s): each
    test gives the app its own database, providers and log records."""
    app = FastAPI()
    app.include_router(stream_router)
    install_error_handlers(app)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, http=request.param, log_level="info", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield app, server, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture
def served(_uvicorn, upstream):
    app, server, base = _uvicorn
    stream.clear_missing_covers()
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"storage_provider": "store", "auth_enabled": False})
    store = _Store()
    registry = FakeRegistry(metadata=[_Covers(upstream)], syncs=[_Sync(upstream)], storage=[store])

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }

    records = _Records()
    watched = [logging.getLogger(name) for name in ("uvicorn.error", "uvicorn", "routes.stream")]
    for logger in watched:
        logger.addHandler(records)
    try:
        yield _Served(base, records, upstream, engine, store, server)
    finally:
        _idle(server)
        for logger in watched:
            logger.removeHandler(records)
        app.dependency_overrides = {}
        stream.clear_missing_covers()


def _idle(server: uvicorn.Server) -> None:
    """Wait until uvicorn has closed every connection and finished every
    request: whatever it logs about a response (an aborted one included) is
    written before that."""
    state = server.server_state
    deadline = time.monotonic() + 5
    while state.connections or state.tasks:
        assert time.monotonic() < deadline, "uvicorn is still serving"
        time.sleep(0.002)


def _get(base: str, path: str) -> tuple[httpx.Response, bytes]:
    """GET with a real client that reads the whole body; raises if the
    transfer breaks. Default Accept-Encoding, like a browser."""
    with httpx.Client(timeout=5) as client:
        with client.stream("GET", base + path) as response:
            body = response.read()
    return response, body


def _settle(served: _Served) -> None:
    """No response broke its headers. uvicorn logs an aborted response right
    after closing the socket: once it is idle, every such log has been
    written, so the check never races it."""
    _idle(served.server)
    assert served.records.runtime_errors() == []


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------


def test_a_gzip_json_error_with_a_compressed_length_is_a_clean_404(served):
    base, records, upstream = served

    response, _ = _get(base, "/cover/provider/covers/gzip-json")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    _settle(served)


def test_the_upstream_is_asked_for_an_uncompressed_body(served):
    base, records, upstream = served

    response, body = _get(base, "/cover/provider/covers/negotiated-image")

    assert response.status_code == 200
    assert body == IMAGE
    assert int(response.headers["content-length"]) == len(IMAGE)
    assert upstream.requests[-1][1].get("accept-encoding") == "identity"
    _settle(served)


def test_a_gzip_image_sent_anyway_arrives_whole_without_the_compressed_length(served):
    base, records, _ = served

    response, body = _get(base, "/cover/provider/covers/gzip-image")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert body == IMAGE
    assert "content-encoding" not in response.headers
    assert response.headers.get("content-length") in (None, str(len(IMAGE)))
    _settle(served)


def test_a_chunked_image_arrives_whole(served):
    base, records, _ = served

    response, body = _get(base, "/cover/provider/covers/chunked-image")

    assert response.status_code == 200
    assert body == IMAGE
    _settle(served)


def test_a_200_with_a_json_error_is_not_served_as_a_cover(served):
    base, records, _ = served

    response, body = _get(base, "/cover/provider/covers/json-error")

    assert response.status_code == 404
    assert b"subsonic-response" not in body
    _settle(served)


def test_a_missing_cover_is_remembered_for_a_while(served):
    base, records, upstream = served

    first, _ = _get(base, "/cover/provider/covers/json-error")
    upstream.hits.acquire(timeout=5)
    asked = len(upstream.requests)
    second, _ = _get(base, "/cover/provider/covers/json-error")

    assert first.status_code == second.status_code == 404
    assert len(upstream.requests) == asked
    _settle(served)


def test_an_upstream_404_is_a_404(served):
    base, records, _ = served

    response, _ = _get(base, "/cover/provider/covers/missing")

    assert response.status_code == 404
    _settle(served)


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def test_an_audio_stream_is_passed_through_with_its_length(served):
    base, records, _ = served

    response, body = _get(base, "/stream/provider/syncfake/audio")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/flac"
    assert int(response.headers["content-length"]) == len(AUDIO)
    assert body == AUDIO
    _settle(served)


def test_a_stream_that_answers_json_is_a_502_before_any_audio(served):
    base, records, _ = served

    response, body = _get(base, "/stream/provider/syncfake/json-error")

    assert response.status_code == 502
    assert response.json()["detail"]["provider"] == "syncfake"
    _settle(served)


@pytest.mark.parametrize("name", ["premature", "premature-chunked"])
def test_an_upstream_that_hangs_up_mid_body_breaks_the_transfer_promptly(served, name):
    """The headers are gone by then: the only honest answer is to cut the
    connection, so the client sees an incomplete transfer (never a
    well-formed truncated file) — at once, not after a timeout."""
    base, records, _ = served

    started = time.monotonic()
    with pytest.raises(httpx.RemoteProtocolError):
        _get(base, f"/stream/provider/syncfake/{name}")

    assert time.monotonic() - started < 3
    _settle(served)


# ---------------------------------------------------------------------------
# /stream/{track_id}: the stored file, else the sync provider's copy
# ---------------------------------------------------------------------------


def _stored_track(served: _Served, *, sync_id: str | None = None) -> str:
    track = make_track(title="Stored")
    with Session(served.engine) as session:
        session.add(TrackRecord(internal_id=track.internal_id, title=track.title, artist=track.artist, album=track.album))
        session.flush()
        session.add(Download(
            track_id=track.internal_id, status="downloaded", file_path="A/stored.flac",
            completed_at=datetime.now(timezone.utc),
        ))
        if sync_id:
            session.add(TrackIdentifierRecord(
                track_id=track.internal_id, id_type=sync_id_type("syncfake"), value=sync_id,
            ))
        session.commit()
    return track.internal_id


def test_a_file_the_storage_lost_is_a_404_not_a_500(served):
    base, records, _ = served
    track_id = _stored_track(served)

    response, _ = _get(base, f"/stream/{track_id}")

    assert response.status_code == 404
    _settle(served)


def test_a_storage_that_does_not_answer_is_a_503(served):
    base, records, _ = served
    served.store.down = True
    track_id = _stored_track(served)

    response, _ = _get(base, f"/stream/{track_id}")

    assert response.status_code == 503
    assert response.headers["retry-after"]
    _settle(served)


def test_a_lost_file_falls_back_to_the_sync_providers_copy(served):
    base, records, _ = served
    track_id = _stored_track(served, sync_id="audio")

    response, body = _get(base, f"/stream/{track_id}")

    assert response.status_code == 200
    assert body == AUDIO
    _settle(served)


def test_the_sync_copy_is_checked_like_any_stream(served):
    base, records, _ = served
    track_id = _stored_track(served, sync_id="json-error")

    response, _ = _get(base, f"/stream/{track_id}")

    assert response.status_code == 502
    _settle(served)
