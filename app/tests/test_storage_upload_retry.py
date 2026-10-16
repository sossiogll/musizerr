"""
A storage upload that times out keeps the downloaded file; the next attempt
uploads it again without downloading it again.

Seen live (2 Oct): "Storage upload failed: timed out" 159 times in a day.
Each attempt downloaded the file, spent ~38 s on the FTP timeout, and threw
the file away with its workspace; after the retries the download failed.

Now (``workers.tasks.download_task`` / ``_store_download``):
- a failed upload moves the downloaded file to the track's held folder
  (``services.held_uploads``) and the task retries;
- the retry finds the held file and does only the storage step;
- a storage that keeps failing rests (the provider cool-down, keyed
  ``storage:<name>``): while it rests the file is kept and the retry waits
  for the end of the rest, without touching the storage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import DownloadProvider
from musizerr_sdk.models import Track
from musizerr_sdk.context import StaticProviderContext
from core.provider_slots import UnlimitedSlots
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from db.models import Download, LibraryEntry
from services import downloads, held_uploads
from services.config_manager import ConfigManager
from services.provider_cooldown import RedisProviderCooldown
from workers import tasks

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRedis, FakeRegistry


class _Downloader(DownloadProvider):
    name = label = "dl"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    supported_identifier_types = {"dl_id"}

    def __init__(self) -> None:
        super().__init__(StaticProviderContext())
        self.downloads = 0

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def download_by_id(self, identifier, workspace, session):
        self.downloads += 1
        (Path(workspace) / "song.flac").write_bytes(b"the audio")
        return "song.flac"


class _FlakyStorage:
    name = "remote"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.stored: dict[str, bytes] = {}

    def store(self, source, relative_dest, session):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("timed out")
        self.stored[relative_dest] = Path(source.path).read_bytes()
        return relative_dest

    def size(self, relative_path, session):
        return len(self.stored.get(relative_path, b""))


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({
        "workspace_dir": str(tmp_path / "work"), "verify_integrity_enabled": False,
        "verify_after_upload": "size", "download_provider_order": ["dl"], "max_retries": 5,
        "provider_cooldown_errors": 3, "provider_cooldown_seconds": 900,
    })
    downloader = _Downloader()
    registry = FakeRegistry(downloads=[downloader])
    redis = FakeRedis()
    queued: list[dict] = []
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_config", lambda: config)
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    monkeypatch.setattr(tasks, "get_registry", lambda: registry)
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "RedisProviderSlots", lambda **kw: UnlimitedSlots())
    monkeypatch.setattr(tasks, "RedisProviderCooldown", lambda **kw: RedisProviderCooldown(client=redis, **kw))
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks.sync_index, "index_track", lambda *a, **k: False)

    track = Track(title="Song", artist="Artist", album="Album", duration_ms=200_000, provider="meta",
                  provider_id="m1", external_ids=[Identifier(type=PROVIDER_ID, value="x1", provider="dl")])
    with Session(engine) as session:
        assert downloads.enqueue_track(track, session, registry=registry).status == downloads.QUEUED
    call = queued[-1]

    def attempt(storage) -> dict | Exception:
        monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (storage, storage.name))
        try:
            return tasks.download_task.run(track_data=call["track_data"], download_id=call["download_id"])
        except Exception as exc:  # self.retry() outside a worker re-raises the failure
            return exc

    def download() -> Download:
        with Session(engine) as session:
            return session.get(Download, call["download_id"])

    return type("Env", (), dict(
        attempt=staticmethod(attempt), download=staticmethod(download), downloader=downloader,
        config=config, track_id=call["track_data"]["internal_id"], engine=engine, redis=redis,
    ))


def test_a_timed_out_upload_keeps_the_file_and_the_retry_only_uploads_it(env):
    storage = _FlakyStorage(failures=1)

    first = env.attempt(storage)
    assert isinstance(first, RuntimeError) and "Storage upload failed: timed out" in str(first)
    held = held_uploads.find(env.config.workspace_dir, env.track_id)
    assert held is not None and Path(held.file().path).read_bytes() == b"the audio"

    second = env.attempt(storage)

    assert second["status"] == ds.DOWNLOADED
    assert env.downloader.downloads == 1  # downloaded once, uploaded twice
    assert storage.calls == 2
    assert list(storage.stored.values()) == [b"the audio"]
    assert env.download().provider_used == "dl"
    assert held_uploads.find(env.config.workspace_dir, env.track_id) is None
    with Session(env.engine) as session:
        assert session.exec(select(LibraryEntry)).one().file_path == second["file_path"]


def test_a_storage_that_keeps_failing_rests_and_the_file_waits_for_it(env):
    storage = _FlakyStorage(failures=100)
    for _ in range(3):  # provider_cooldown_errors
        env.attempt(storage)
    assert storage.calls == 3

    resting = env.attempt(storage)

    assert "storage resting" in str(resting)
    assert storage.calls == 3  # not even tried while it rests
    assert env.downloader.downloads == 1
    assert held_uploads.find(env.config.workspace_dir, env.track_id) is not None


def test_an_exhausted_upload_stays_retriable_with_its_file(env, monkeypatch):
    env.config.update({"max_retries": 0, "provider_cooldown_errors": 0})
    storage = _FlakyStorage(failures=1)

    result = env.attempt(storage)

    assert result["status"] == ds.FAILED
    assert env.download().retriable is True
    assert held_uploads.find(env.config.workspace_dir, env.track_id) is not None


def test_a_held_file_is_kept_by_the_workspace_sweep_and_dropped_after_its_max_age(env, tmp_path):
    import os
    import time

    from services.workspace import remove_stale_workspaces

    env.attempt(_FlakyStorage(failures=1))
    root = Path(env.config.workspace_dir)
    old = time.time() - 10 * 86400
    for entry in root.iterdir():
        os.utime(entry, (old, old))
    held_dir = root / "held-uploads"
    for entry in held_dir.iterdir():
        os.utime(entry, (old, old))

    assert remove_stale_workspaces(root, 3600) == 0
    assert held_uploads.find(root, env.track_id) is not None
    assert held_uploads.remove_stale(root, 72 * 3600) == 1
    assert held_uploads.find(root, env.track_id) is None

