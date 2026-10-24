"""
L'API e i worker scrivono sullo stesso file SQLite.

- Ogni connessione (``db.engine.make_engine``, usato da API e worker) usa
  WAL e un ``busy_timeout``: una scrittura dell'API aspetta il lock di un
  worker invece di fallire subito.
- Se l'attesa scade, la risposta è un 503 riprovabile (``database_busy``),
  mai un 500.
- La scansione dei duplicati non tiene un lock di scrittura durante l'I/O di
  rete.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

import main
from core import user_auth
from musizeer_sdk.auth import AuthSession
from db import engine as db_engine
from db.engine import get_session, make_engine, sqlite_url
from db.models import User
from routes import deps
from services.config_manager import ConfigManager

from tests.conftest import (
    FakeIdentificationProvider,
    FakeRegistry,
    FakeSyncProvider,
    encode_identity_bytes,
    make_library_item,
)
from tests.test_openapi_responses import assert_conforms

pytestmark = pytest.mark.sqlite_only("the SQLite file lock, its busy timeout and its pragmas")


def _pragma(engine, name):
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_every_connection_gets_wal_and_a_busy_timeout(tmp_path):
    engine = make_engine(sqlite_url(tmp_path / "m.db"), busy_timeout_ms=1234)

    assert _pragma(engine, "journal_mode") == "wal"
    assert _pragma(engine, "busy_timeout") == 1234
    assert _pragma(engine, "synchronous") == 1  # NORMAL


def test_the_default_timeout_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIZEER_DB_BUSY_TIMEOUT_MS", "2500")
    assert _pragma(make_engine(sqlite_url(tmp_path / "m.db")), "busy_timeout") == 2500

    monkeypatch.setenv("MUSIZEER_DB_BUSY_TIMEOUT_MS", "not a number")
    assert _pragma(make_engine(sqlite_url(tmp_path / "n.db")), "busy_timeout") == db_engine.DEFAULT_BUSY_TIMEOUT_MS


def test_api_and_workers_share_the_configured_engine(tmp_path):
    from workers import duplicates, maintenance, scheduler, tasks

    for module in (duplicates, maintenance, scheduler, tasks):
        assert module.engine is db_engine.engine

    # Il motore del modulo, costruito all'import come in produzione.
    probe = (
        "from db.engine import engine\n"
        "with engine.connect() as c:\n"
        "    print(c.exec_driver_sql('PRAGMA journal_mode').scalar(),"
        " c.exec_driver_sql('PRAGMA busy_timeout').scalar())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=Path(db_engine.__file__).resolve().parent.parent,
        env={**os.environ, "MUSIZEER_DATA_DIR": str(tmp_path), "MUSIZEER_DB_BUSY_TIMEOUT_MS": "4321"},
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == ["wal", "4321"]


# ---------------------------------------------------------------------------
# Scrittore concorrente + login
# ---------------------------------------------------------------------------

def _hold_write_lock(path, started: threading.Event, release: threading.Event, at_most: float):
    """Un altro processo (un worker) che tiene il lock di scrittura finché il
    test non lo rilascia (``release``) o per ``at_most`` secondi: il lock
    copre tutta la richiesta sotto esame, senza attese fisse dopo."""
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS worker_scratch (x INTEGER)")
        conn.execute("INSERT INTO worker_scratch VALUES (1)")
        started.set()
        release.wait(at_most)
        conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def _writer_holding_the_lock(path, *, at_most: float = 10):
    """Il lock di scrittura tenuto da un altro scrittore per tutto il blocco
    (al massimo ``at_most`` secondi), poi rilasciato."""
    started, release = threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_write_lock, args=(path, started, release, at_most))
    writer.start()
    assert started.wait(2), "the writer never took the lock"
    try:
        yield
    finally:
        release.set()
        writer.join()


@pytest.fixture
def login_app(tmp_path):
    installed = []

    def _make(busy_timeout_ms):
        path = tmp_path / f"api-{busy_timeout_ms}.db"
        engine = make_engine(sqlite_url(path), busy_timeout_ms=busy_timeout_ms)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(User(username="alice", password_hash=user_auth.hash_password("pw"), role="user"))
            session.commit()
        config = ConfigManager(engine=engine)
        config.update({"auth_enabled": True})

        def _session():
            with Session(engine) as s:
                yield s

        overrides = {get_session: _session, deps.get_config: lambda: config}
        main.app.dependency_overrides.update(overrides)
        installed.extend(overrides)
        return TestClient(main.app, raise_server_exceptions=False), path

    yield _make
    for key in installed:
        main.app.dependency_overrides.pop(key, None)


def test_login_waits_for_a_concurrent_writer(login_app):
    client, path = login_app(busy_timeout_ms=5_000)
    # The writer lets go on its own while the login is waiting for it.
    with _writer_holding_the_lock(path, at_most=0.5):
        resp = client.post("/auth/account/login", json={"username": "alice", "password": "pw"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


def test_a_lock_that_outlasts_the_timeout_is_a_retryable_503(login_app):
    client, path = login_app(busy_timeout_ms=100)
    with _writer_holding_the_lock(path):
        resp = client.post("/auth/account/login", json={"username": "alice", "password": "pw"})

    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"].isdigit()
    assert resp.json()["detail"]["error_code"] == "database_busy"
    assert_conforms(resp, "POST", "/auth/account/login")


def test_reads_are_not_blocked_by_a_writer(login_app):
    client, path = login_app(busy_timeout_ms=100)
    token = None
    resp = client.post("/auth/account/login", json={"username": "alice", "password": "pw"})
    token = resp.json()["access_token"]
    with _writer_holding_the_lock(path):
        me = client.get("/auth/account/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200


# ---------------------------------------------------------------------------
# Worker: nessun lock di scrittura durante l'I/O di rete
# ---------------------------------------------------------------------------

class _ProbingSync(FakeSyncProvider):
    """Durante ogni fetch (I/O di rete), un'altra connessione prova a
    scrivere senza attendere: riesce solo se il worker non tiene il lock."""

    def __init__(self, db_path, **kwargs):
        super().__init__("fakesync", **kwargs)
        self._db_path = db_path
        self.write_probes: list[bool] = []

    def fetch_partial(self, sync_id, workspace, session, max_bytes=None):
        conn = sqlite3.connect(self._db_path, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            self.write_probes.append(True)
        except sqlite3.OperationalError:
            self.write_probes.append(False)
        finally:
            conn.close()
        return super().fetch_partial(sync_id, workspace, session, max_bytes)


def test_the_duplicates_scan_never_holds_a_write_lock_across_network_io(tmp_path, monkeypatch):
    from workers import duplicates

    path = tmp_path / "worker.db"
    engine = make_engine(sqlite_url(path), busy_timeout_ms=100)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(duplicates, "engine", engine)
    monkeypatch.setattr(duplicates, "auth_manager_for", lambda store: type(
        "_A", (), {"authenticate": lambda self, p: AuthSession.anonymous()})())

    items = [
        make_library_item("Song", "Artist", "Album", sync_id=f"sid-{i}", path=f"v/{i}.flac", size=1000 + i)
        for i in range(3)
    ]
    sync = _ProbingSync(path, library=items)
    for item in items:
        sync.files[item.sync_id] = encode_identity_bytes("rec-1")

    settings_engine = make_engine(sqlite_url(tmp_path / "settings.db"))
    SQLModel.metadata.create_all(settings_engine)
    config = ConfigManager(engine=settings_engine)
    config.update({
        "dedup_enabled": True, "identification_enabled": True,
        "sync_provider": "fakesync", "workspace_dir": str(tmp_path / "ws"),
    })
    registry = FakeRegistry(syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)])
    monkeypatch.setattr(duplicates, "get_config", lambda: config)
    monkeypatch.setattr(duplicates, "get_registry", lambda: registry)

    result = duplicates.scan_library_duplicates()

    assert result["status"] == "done" and result["groups_created"] == 1
    assert sync.write_probes == [True, True, True]
