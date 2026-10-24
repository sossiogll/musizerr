"""
Il probe full-sync scrive un file di prova tramite lo storage provider (con
il contratto a cartelle di lavoro: il file arriva come ``WorkspaceFile``),
fa scansionare il sync provider e, se il file compare, attiva ``mode: full``.
Il file di prova e la cartella di lavoro spariscono sempre.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from musizeer_sdk.interfaces import SyncCandidate
from core.settings_resolver import namespace_provider_key
from db.settings_store import SettingsRepository
from services.config_manager import ConfigManager
from workers import maintenance

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeStorageProvider, FakeSyncProvider


class _MirrorSync(FakeSyncProvider):
    """Sync provider che "vede" i file che lo storage ha ricevuto: la
    libreria condivisa di un full-sync reale."""

    def __init__(self, storage: FakeStorageProvider) -> None:
        super().__init__("mirror")
        self._storage = storage

    def find_candidates(self, track, session):
        self.availability_calls += 1
        seen = any(track.title in path for path in self._storage.files)
        return [SyncCandidate(sync_id="probe", track=track)] if seen else []


class _BlindSync(FakeSyncProvider):
    def __init__(self) -> None:
        super().__init__("blind")


class _Clock:
    """The probe's clock: ``sleep`` moves time forward at once, so waiting
    out the probe timeout takes no real time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


TIMEOUT, INTERVAL = 30, 5


@pytest.fixture
def run(monkeypatch, _workspace_dir_in_tmp):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({
        "storage_provider": "fakestorage",
        "full_sync_probe_timeout_seconds": TIMEOUT,
        "full_sync_probe_interval_seconds": INTERVAL,
    })
    monkeypatch.setattr(maintenance, "time", _Clock())
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: FakeAuthManager())

    def _run(storage, sync):
        monkeypatch.setattr(maintenance, "get_registry", lambda: FakeRegistry(syncs=[sync], storage=[storage]))
        result = maintenance.verify_full_sync(sync.name)
        with Session(engine) as session:
            mode = SettingsRepository(session).get_system(namespace_provider_key(sync.name, "mode"))
        return result, mode

    return _run, _workspace_dir_in_tmp


def test_a_shared_library_turns_full_sync_on_and_leaves_nothing_behind(run):
    _run, workspace_root = run
    storage = FakeStorageProvider()

    result, mode = _run(storage, _MirrorSync(storage))

    assert result == {"status": "done", "full_sync": True, "sync_provider": "mirror"}
    assert mode == "full"
    assert len(storage.deleted) == 1 and storage.deleted[0].startswith("Musizeer Probe/")
    assert list(workspace_root.glob("*/*")) == [], "no call workspace is left (only the provider folders)"


def test_a_library_the_sync_provider_cannot_see_stays_off(run):
    _run, workspace_root = run
    storage = FakeStorageProvider()
    sync = _BlindSync()

    result, mode = _run(storage, sync)

    assert result["full_sync"] is False
    assert mode is None
    # Asked every interval until the timeout, then given up.
    assert sync.availability_calls == TIMEOUT // INTERVAL
    assert len(storage.deleted) == 1
    assert list(workspace_root.glob("*/*")) == [], "no call workspace is left (only the provider folders)"
