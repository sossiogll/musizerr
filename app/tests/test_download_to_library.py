"""
Dalla cartella di lavoro alla libreria (ADR-0003, workers/tasks.py
``_store_download``): il provider scrive solo nella cartella che l'host gli
dà e restituisce un nome; l'host verifica il file, decide il path in
libreria e lo consegna allo storage.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from core.download_resolver import ResolveResult
from musizerr_sdk.interfaces import WorkspaceFile
from core.provider_cooldown import NoCooldown
from services import held_uploads
from services.workspace import open_workspace
from workers import tasks
from services.library_paths import library_path
from workers.tasks import _register_library_entry, _store_download, _Storing

from tests.conftest import FakeAuthManager, FakeRegistry, make_track


class _RecordingStorage:
    name = "fakestorage"

    def __init__(self, *, fail: bool = False, stored_size: int | None = None) -> None:
        self.stored: list[tuple[WorkspaceFile, str, bytes]] = []
        self.deleted: list[str] = []
        self._fail = fail
        self._stored_size = stored_size

    def store(self, source, relative_dest, session):
        if self._fail:
            raise OSError("remote unreachable")
        with open(source.path, "rb") as f:
            self.stored.append((source, relative_dest, f.read()))
        return relative_dest

    def size(self, relative_path, session):
        return self._stored_size

    def delete(self, relative_path, session):
        self.deleted.append(relative_path)
        return True


def _storing(storage):
    return _Storing(storage, "fakestorage", FakeAuthManager(), NoCooldown())


def _config(**overrides):
    base = dict(
        track_format="{artist}/{album}/{title}",
        verify_integrity_enabled=False,
        anti_mislabel_enabled=False,
        identification_enabled=False,
        verify_after_upload="off",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(tasks, "_update_download_status", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)


TRACK = make_track(title="Song", artist="Artist", album="Album")


def _run(tmp_path, storage, *, name="song.flac", content=b"audio", config=None):
    result = ResolveResult(success=True, provider="fakedl", file_name=name)
    with open_workspace(tmp_path, "fakedl") as ws:
        if content is not None:
            (ws.path / "song.flac").write_bytes(content)
        stored = _store_download(
            result, ws, TRACK, _storing(storage), FakeRegistry(), config or _config(workspace_dir=tmp_path),
            download_id="dl-1", retry_num=0,
        )
        workspace_path = ws.path
    return result, stored, workspace_path


def test_the_host_decides_the_library_path_from_the_template_and_the_file_extension(tmp_path):
    storage = _RecordingStorage()

    result, stored, _ = _run(tmp_path, storage)

    assert result.success is True
    assert stored == "Artist/Album/Song.flac"
    source, relative_dest, content = storage.stored[0]
    assert source.name == "song.flac"
    assert relative_dest == "Artist/Album/Song.flac"
    assert content == b"audio"


def test_the_workspace_is_gone_after_the_operation(tmp_path):
    _, _, workspace_path = _run(tmp_path, _RecordingStorage())

    assert not workspace_path.exists()


@pytest.mark.parametrize("name", ["../escape.flac", "missing.flac"])
def test_a_name_outside_the_workspace_fails_without_storing_or_retrying(tmp_path, name):
    storage = _RecordingStorage()

    result, stored, _ = _run(tmp_path, storage, name=name)

    assert stored is None
    assert result.success is False and result.retriable is False
    assert storage.stored == []


def test_a_storage_failure_is_retriable_and_keeps_the_file(tmp_path):
    result, stored, _ = _run(tmp_path, _RecordingStorage(fail=True))

    assert stored is None
    assert result.success is False and result.retriable is True
    held = held_uploads.find(tmp_path, TRACK.internal_id)
    assert held is not None and held.provider == "fakedl"
    assert open(held.file().path, "rb").read() == b"audio"


def test_size_verification_uses_the_size_measured_before_the_storage_moved_the_file(tmp_path):
    storage = _RecordingStorage(stored_size=len(b"audio"))

    result, stored, _ = _run(tmp_path, storage, config=_config(verify_after_upload="size", workspace_dir=tmp_path))

    assert result.success is True and stored == "Artist/Album/Song.flac"


def test_a_size_mismatch_deletes_the_stored_copy_and_is_retriable(tmp_path):
    storage = _RecordingStorage(stored_size=1)

    result, stored, _ = _run(tmp_path, storage, config=_config(verify_after_upload="size", workspace_dir=tmp_path))

    assert stored is None
    assert result.retriable is True
    assert storage.deleted == ["Artist/Album/Song.flac"]
    assert held_uploads.find(tmp_path, TRACK.internal_id) is not None


def test_no_storage_provider_fails_without_retrying(tmp_path):
    result, stored, _ = _run(tmp_path, None)

    assert stored is None
    assert result.success is False and result.retriable is False


def test_library_path_falls_back_when_the_template_has_an_unknown_placeholder():
    track = make_track(title="T", artist="A", album="B")

    assert library_path(track, "{nope}", ".mp3") == "A/B/T.mp3"


def test_register_library_entry_has_no_hardcoded_storage_provider_default():
    params = inspect.signature(_register_library_entry).parameters
    assert params["storage_provider"].default is inspect.Parameter.empty


def test_anti_mislabel_reads_the_downloaded_file_from_the_workspace(tmp_path):
    from tests.conftest import FakeIdentificationProvider, encode_identity_bytes

    identification = FakeIdentificationProvider(available=True)
    storage = _RecordingStorage()
    track = make_track(title="Song", artist="Artist", album="Album")
    result = ResolveResult(success=True, provider="fakedl", file_name="song.flac")
    config = _config(anti_mislabel_enabled=True, identification_enabled=True, identification_confidence_threshold=0.9)
    with open_workspace(tmp_path, "fakedl") as ws:
        (ws.path / "song.flac").write_bytes(encode_identity_bytes("rec-x", title="Different", score=0.99))
        stored = _store_download(
            result, ws, track, _storing(storage), FakeRegistry(identification=[identification]), config,
            download_id="dl-1", retry_num=0,
        )

    assert identification.identify_calls == 1
    assert stored is None and storage.stored == []
    assert "mislabel" in result.error
