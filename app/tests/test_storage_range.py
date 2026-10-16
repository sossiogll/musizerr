"""
Listening to a track stored on a remote storage: a storage that hands the
bytes as they arrive (``chunks``, with the file size) and that declares
``supports_stream_offset`` restarts the transfer at the offset of a Range
request (a seek); the host passes the offset and answers 206. A storage that
cannot seek hands the whole file and the host does not pretend otherwise.

How a real storage opens its transfer is tested in its own repository; here
the host's answer, with a generic storage that serves chunks.
"""
from __future__ import annotations

import asyncio

from fastapi.responses import StreamingResponse

from musizerr_sdk.interfaces import StorageStreamResult
from routes.stream import _stream_stored, _stored_file_response
from services.workspace import new_workspace

from tests.conftest import FakeStorageProvider
from tests.loopback import as_proxy

AUDIO = bytes(range(256)) * 1024  # 256 KiB
BLOCK = 8192


class _ChunkStorage(FakeStorageProvider):
    """Serves its file as blocks, from the offset asked, and says when the
    consumer left before the end."""

    supports_stream_offset = True

    def __init__(self) -> None:
        super().__init__("remote")
        self.files = {"A/song.flac": AUDIO}
        self.offsets: list[int | None] = []
        self.transferred = 0

    def stream(self, relative_path, workspace, session, offset=None):
        data = self.files[relative_path]
        self.offsets.append(offset)
        start = offset or 0

        def blocks():
            sent = start
            while sent < len(data):
                block = data[sent:sent + BLOCK]
                sent += len(block)
                self.transferred += len(block)
                yield block

        return StorageStreamResult(chunks=blocks(), media_type="audio/flac", size=len(data), offset=start)


class _Config:
    def __init__(self, root):
        self.workspace_dir = root


def _body(response: StreamingResponse) -> bytes:
    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])
    return asyncio.run(collect())


def test_the_host_answers_a_range_with_206_from_the_offset(tmp_path):
    storage = _ChunkStorage()

    response = _stream_stored(as_proxy(storage), None, "A/song.flac", _Config(tmp_path), "bytes=1000-")

    assert storage.offsets == [1000]  # the seek is the storage's: it restarts there
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 1000-{len(AUDIO) - 1}/{len(AUDIO)}"
    assert response.headers["content-length"] == str(len(AUDIO) - 1000)
    assert response.headers["accept-ranges"] == "bytes"
    assert _body(response) == AUDIO[1000:]


def test_the_host_answers_the_whole_file_with_its_length(tmp_path):
    storage = _ChunkStorage()

    response = _stream_stored(as_proxy(storage), None, "A/song.flac", _Config(tmp_path), None)

    assert storage.offsets == [None]
    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(AUDIO))
    assert _body(response) == AUDIO


def test_a_range_past_the_end_is_416(tmp_path):
    response = _stream_stored(as_proxy(_ChunkStorage()), None, "A/song.flac", _Config(tmp_path), f"bytes={len(AUDIO)}-")

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(AUDIO)}"


def test_chunks_from_a_storage_that_cannot_seek_ignore_the_range(tmp_path):
    ws = new_workspace(tmp_path, "remote")
    result = StorageStreamResult(chunks=iter([b"ab", b"cd"]), media_type="audio/mpeg", size=4)

    response = _stored_file_response(result, ws, "bytes=2-", provider="store")

    assert response.status_code == 200
    assert "accept-ranges" not in response.headers
    assert response.headers["content-length"] == "4"
