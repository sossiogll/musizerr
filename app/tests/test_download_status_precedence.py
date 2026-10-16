"""
One file state per track, decided by the server, carried by the list AND by
the live events.

Real case (29 Sep): a track whose latest attempt was "dispatched" sat in the
"Completed" tab (an older attempt had landed: it is in the library) but the
row showed "Delegated", because the UI preferred the live event's raw
attempt status over the server's aggregate; a live "failed" offered "Retry"
on a track in the library. The worker also set ``completed_at`` on a
dispatch (not terminal) and left it empty on a failure (terminal).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from core.download_resolver import ResolveResult
from db.download_store import status_summary
from db.models import Download, LibraryEntry, TrackRecord
from services import progress
from services.config_manager import ConfigManager
from services.track_state import file_state_now
from workers import tasks

from tests.database import new_engine
from tests.conftest import FakeRegistry, make_track


@pytest.fixture
def engine(monkeypatch):
    eng = new_engine()
    monkeypatch.setattr(tasks, "engine", eng)
    monkeypatch.setattr(progress, "engine", eng)
    return eng


def _record(engine, title="Song") -> str:
    with Session(engine) as session:
        record = TrackRecord(title=title, artist="Artist", album="Album", duration_ms=1000)
        session.add(record)
        session.commit()
        return record.internal_id


def _download(engine, track_id: str, status: str, **fields) -> str:
    with Session(engine) as session:
        dl = Download(track_id=track_id, status=status, **fields)
        session.add(dl)
        session.commit()
        return dl.id


def _row(engine, download_id: str) -> Download:
    with Session(engine) as session:
        return session.get(Download, download_id)


# ------------------------------------------------------------------
# completed_at iff terminal
# ------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(ds.ORDER))
def test_completed_at_is_set_iff_the_state_is_terminal(engine, status):
    track_id = _record(engine)
    download_id = _download(engine, track_id, ds.DOWNLOADING, completed_at=datetime(2026, 9, 1))

    tasks._update_download_status(download_id, status=status)

    assert (_row(engine, download_id).completed_at is not None) == (status in ds.TERMINAL)


@pytest.fixture
def run_task(engine, monkeypatch):
    cm = ConfigManager(engine=engine)
    cm.update({"max_retries": 0})
    monkeypatch.setattr(tasks, "get_config", lambda: cm)
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (None, "disk"))
    monkeypatch.setattr("workers.maintenance.start_reconciliation", lambda *a, **k: True)

    def run(result: ResolveResult) -> Download:
        track_id = _record(engine)
        download_id = _download(engine, track_id, ds.PENDING)
        monkeypatch.setattr(tasks.DownloadResolver, "resolve", lambda self, track, workspace, on_trying=None: result)
        track = make_track()
        track.internal_id = track_id
        tasks.download_task.run(track_data=track.to_dict(), download_id=download_id)
        return _row(engine, download_id)

    return run


def test_a_dispatch_is_not_a_completion(run_task):
    row = run_task(ResolveResult(success=False, delegated=True, provider="ext", delegated_handle="7"))

    assert row.status == ds.DISPATCHED
    assert row.completed_at is None


@pytest.mark.parametrize("retriable", [False, True])
def test_a_failure_is_a_completion(run_task, retriable):
    row = run_task(ResolveResult(success=False, error="No provider could download 'Song'", retriable=retriable))

    assert row.status == ds.FAILED
    assert row.completed_at is not None
    # Kept for the routine that retries transient failures later.
    assert row.retriable is retriable


# ------------------------------------------------------------------
# The file state: in the library beats every attempt
# ------------------------------------------------------------------

def _in_library(engine, track_id: str) -> None:
    with Session(engine) as session:
        session.add(LibraryEntry(track_id=track_id, file_path=f"{track_id}.ogg", file_format="ogg",
                                 source="scan", storage_provider="disk"))
        session.commit()


@pytest.mark.parametrize("live", sorted(ds.ORDER))
def test_a_live_event_carries_the_file_state_of_its_track(engine, monkeypatch, live):
    sent: list[dict] = []
    monkeypatch.setattr(progress, "_publish", lambda payload: sent.append(json.loads(payload)))
    owned, missing = _record(engine, "Owned"), _record(engine, "Missing")
    _in_library(engine, owned)
    _download(engine, missing, live)

    progress.publish_download_progress("dl-1", live, "Owned", owned, FakeRegistry())
    progress.publish_download_progress("dl-2", live, "Missing", missing, FakeRegistry())

    assert sent[0]["status"] == live and sent[0]["file_state"] == ds.DOWNLOADED
    assert sent[1]["file_state"] == live
    assert sent[1]["phase"] == ds.LABELS[live]["phase"]


def test_a_track_in_the_library_is_done_in_the_downloads_counts_whatever_its_attempts(engine):
    owned = _record(engine, "Owned")
    _in_library(engine, owned)
    _download(engine, owned, ds.DOWNLOADED, completed_at=datetime(2026, 9, 7))
    _download(engine, owned, ds.DISPATCHED, delegation_handle="7513")  # requested again, 28 Sep
    scanned = _record(engine, "Scanned")
    _in_library(engine, scanned)
    _download(engine, scanned, ds.FAILED, completed_at=datetime(2026, 9, 7))

    with Session(engine) as session:
        summary = status_summary(session, None, since=datetime(2026, 1, 1), sync_id_types=[])
        assert file_state_now(session, owned, FakeRegistry()) == ds.DOWNLOADED
        assert file_state_now(session, scanned, FakeRegistry()) == ds.DOWNLOADED

    assert summary["phases"]["done"] == 2
    assert summary["phases"]["active"] == 0 and summary["phases"]["failed"] == 0


def test_the_downloads_list_shows_a_track_in_the_library_as_done_and_never_as_active(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.user_auth import create_access_token
    from db.engine import get_session
    from db.models import User
    from routes import deps
    from routes.history import router

    eng = new_engine()
    owned = _record(eng, "Owned")
    _in_library(eng, owned)
    _download(eng, owned, ds.DOWNLOADED, completed_at=datetime(2026, 9, 7), started_at=datetime(2026, 9, 7))
    _download(eng, owned, ds.DISPATCHED, delegation_handle="7513", started_at=datetime(2026, 9, 28))
    with Session(eng) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        session.refresh(admin)

    app = FastAPI()
    app.include_router(router)

    def _session():
        with Session(eng) as s:
            yield s

    config = ConfigManager(engine=eng)
    config.update({"auth_enabled": True})
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    app.dependency_overrides[deps.get_auth_manager] = lambda: object()
    headers = {"Authorization": f"Bearer {create_access_token(admin.id, admin.role)}"}
    with TestClient(app) as client:
        done = client.get("/downloads?phase=done", headers=headers).json()
        active = client.get("/downloads?phase=active", headers=headers).json()

    assert [item["track_id"] for item in done["items"]] == [owned]
    assert done["items"][0]["status"] == ds.DOWNLOADED
    assert done["items"][0]["track"]["download_status"] == ds.DOWNLOADED
    assert active["items"] == []
