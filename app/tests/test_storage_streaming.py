"""
Streaming di un file salvato (ADR-0003): lo storage rende un file nella
cartella di lavoro, un URL o blocchi di byte; l'host risponde di conseguenza
e rimuove la cartella a risposta inviata.
"""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from musizerr_sdk.interfaces import StorageStreamResult
from routes.stream import _stored_file_response
from services.provider_calls import ProviderCallError
from services.workspace import new_workspace


def _run_background(response):
    asyncio.run(response.background())


def test_a_file_result_is_served_with_range_support_and_the_workspace_is_removed_after(tmp_path):
    ws = new_workspace(tmp_path, "store")
    (ws.path / "song.flac").write_bytes(b"x")

    response = _stored_file_response(StorageStreamResult(file="song.flac", media_type="audio/flac"), ws, provider="store")

    assert isinstance(response, FileResponse)
    assert response.headers["accept-ranges"] == "bytes"
    _run_background(response)
    assert not ws.path.exists()


def test_a_chunk_result_is_streamed_and_the_workspace_is_removed_after(tmp_path):
    ws = new_workspace(tmp_path, "store")

    response = _stored_file_response(StorageStreamResult(chunks=iter([b"a", b"b"])), ws, provider="store")

    assert isinstance(response, StreamingResponse)
    _run_background(response)
    assert not ws.path.exists()


def test_a_file_name_outside_the_workspace_is_a_404(tmp_path):
    ws = new_workspace(tmp_path / "work", "store")
    (tmp_path / "secret").write_bytes(b"x")

    with pytest.raises(HTTPException) as exc:
        _stored_file_response(StorageStreamResult(file="../../secret"), ws, provider="store")
    assert exc.value.status_code == 404


def test_an_empty_result_is_a_404(tmp_path):
    with pytest.raises(HTTPException) as exc:
        _stored_file_response(StorageStreamResult(), new_workspace(tmp_path, "store"), provider="store")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Proxy di uno stream esterno
# ---------------------------------------------------------------------------


class _Chunks(httpx.SyncByteStream):
    def __init__(self, threads: list) -> None:
        self.threads = threads
        self.closed = False

    def __iter__(self):
        self.threads.append(threading.get_ident())
        yield b"par"
        yield b"tial"

    def close(self):
        self.closed = True


def _upstream(monkeypatch, handler):
    from routes import stream

    clients = []

    def factory():
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(stream, "_upstream_client", factory)
    return clients


async def _drain(response):
    return b"".join([chunk async for chunk in response.body_iterator])


def test_a_range_request_reaches_the_origin_and_its_partial_answer_comes_back(monkeypatch):
    from routes.stream import AUDIO_TYPES, _proxy_stream

    seen = {}
    threads: list = []

    def handler(request):
        seen["range"] = request.headers.get("range")
        return httpx.Response(
            206, headers={"content-type": "audio/flac", "content-range": "bytes 10-16/100", "content-length": "7"},
            stream=_Chunks(threads),
        )

    _upstream(monkeypatch, handler)
    response = _proxy_stream("http://sync/s1", provider="sync", accept=AUDIO_TYPES, range_header="bytes=10-")

    body = asyncio.run(_drain(response))

    assert seen["range"] == "bytes=10-"
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 10-16/100"
    assert body == b"partial"
    assert threads and threads[0] != threading.get_ident()  # letto in un thread, non nell'event loop


@pytest.mark.parametrize("upstream_status, expected", [(404, 404), (500, 502), (429, 503)])
def test_an_upstream_error_is_a_clean_http_error_and_nothing_stays_open(monkeypatch, upstream_status, expected):
    from routes.stream import AUDIO_TYPES, _proxy_stream

    streams: list = []

    def handler(request):
        chunks = _Chunks([])
        streams.append(chunks)
        return httpx.Response(upstream_status, stream=chunks)

    clients = _upstream(monkeypatch, handler)

    with pytest.raises(ProviderCallError) as failure:
        _proxy_stream("http://sync/s1", provider="sync", accept=AUDIO_TYPES)

    assert failure.value.status_code == expected
    assert failure.value.provider == "sync"
    assert streams[0].closed and clients[0].is_closed


@pytest.mark.parametrize("content_type, accept, expected", [
    ("application/json", "audio", 502),
    ("text/html; charset=utf-8", "audio", 502),
    ("audio/flac", "image", 404),
])
def test_a_body_of_the_wrong_type_is_refused_before_any_header(monkeypatch, content_type, accept, expected):
    from routes.stream import AUDIO_TYPES, IMAGE_TYPES, _proxy_stream

    streams: list = []

    def handler(request):
        chunks = _Chunks([])
        streams.append(chunks)
        return httpx.Response(200, headers={"content-type": content_type}, stream=chunks)

    clients = _upstream(monkeypatch, handler)
    kind = {"audio": (AUDIO_TYPES, "upstream_error"), "image": (IMAGE_TYPES, "not_found")}[accept]

    with pytest.raises(ProviderCallError) as failure:
        _proxy_stream("http://origin/x", provider="origin", accept=kind[0], unexpected_kind=kind[1])

    assert failure.value.status_code == expected
    assert streams[0].closed and clients[0].is_closed


def test_octet_stream_passes_only_when_declared(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"bytes")

    _upstream(monkeypatch, handler)

    declared = _stored_file_response(
        StorageStreamResult(url="http://store/f", media_type="application/octet-stream"),
        new_workspace(tmp_path, "store"), provider="store",
    )
    assert asyncio.run(_drain(declared)) == b"bytes"
    with pytest.raises(ProviderCallError):
        _stored_file_response(
            StorageStreamResult(url="http://store/f", media_type="audio/flac"), new_workspace(tmp_path, "store"), provider="store",
        )
