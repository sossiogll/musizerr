"""
The library jobs (``services/library_jobs.py``): "Sync now" and "Download
missing" as one state machine per user and kind.

- the trigger persists a ``queued`` run BEFORE enqueuing, and is idempotent
  (a double click returns the same run);
- the worker moves it queued → running → completed/failed, and never revives
  a run closed as stale;
- progress (phase, provider, processed/total, per-provider entries,
  heartbeat) is written throttled, in short transactions, and published on
  the progress stream for the owner only;
- stale runs become failed with ``worker_unavailable`` / ``worker_lost``;
- guests get 401, a user sees only their runs, the raw error is for admins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.user_auth import create_access_token
from db import library_job_store as store
from db.engine import get_session
from db.models import LibraryJobRun, User, UserSavedTrack
from routes import deps
from routes.library import router as library_router
from routes.system.progress import event_visible_to
from services import downloads, library_jobs
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, make_track


@pytest.fixture
def events(monkeypatch):
    """Every event published on the progress stream (no Redis)."""
    published: list[dict] = []
    monkeypatch.setattr(library_jobs, "publish_event", published.append)
    return published


@pytest.fixture
def alive(monkeypatch):
    """The worker's liveness as the service reads it; True unless a test says otherwise."""
    state = {"value": True}
    monkeypatch.setattr(library_jobs, "worker_alive", lambda queue: state["value"])
    return state


def _user(engine, name: str, role: str = "user") -> User:
    with Session(engine) as session:
        user = User(username=name, password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _run(engine, run_id: str) -> LibraryJobRun:
    with Session(engine) as session:
        return session.get(LibraryJobRun, run_id)


class _Dispatcher:
    """Records the enqueued tasks and what the database held at that moment."""

    def __init__(self, engine, fail: bool = False):
        self.engine = engine
        self.fail = fail
        self.calls: list[tuple[str, dict, str]] = []
        self.rows_at_dispatch: list[tuple[str, str | None]] = []

    def __call__(self, task_name: str, kwargs: dict, task_id: str) -> None:
        run = _run(self.engine, kwargs["run_id"])
        self.rows_at_dispatch.append((run.status, run.task_id))
        if self.fail:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        self.calls.append((task_name, kwargs, task_id))


@pytest.fixture
def dispatcher(engine, monkeypatch):
    d = _Dispatcher(engine)
    monkeypatch.setattr(library_jobs, "_dispatch", d)
    return d


@pytest.fixture
def world(engine, events, alive, dispatcher):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    app = FastAPI()
    app.include_router(library_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    users = {name: _user(engine, name, role) for name, role in (("alice", "user"), ("bob", "user"), ("root", "admin"))}
    headers = {name: {"Authorization": f"Bearer {create_access_token(u.id, u.role)}"} for name, u in users.items()}
    with TestClient(app) as client:
        yield {"client": client, "users": users, "headers": headers, "engine": engine,
               "events": events, "alive": alive, "dispatcher": dispatcher}


def _age(engine, run_id: str, *, created: timedelta | None = None, heartbeat: timedelta | None = None) -> None:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        run = session.get(LibraryJobRun, run_id)
        if created is not None:
            run.created_at = now - created
        if heartbeat is not None:
            run.started_at = now - heartbeat
            run.heartbeat_at = now - heartbeat
        session.add(run)
        session.commit()


# ---------------------------------------------------------------------------
# Trigger: queued before enqueue, idempotent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path, kind, task", [
    ("/library/sync", "library_sync", "workers.tasks.sync_saved_tracks_for_user"),
    ("/library/download-sync?time_range=3m", "download_sync", "workers.tasks.library_download_sync"),
])
def test_the_trigger_persists_a_queued_run_before_enqueuing(world, path, kind, task):
    resp = world["client"].post(path, headers=world["headers"]["alice"])

    assert resp.status_code == 202
    body = resp.json()
    assert body["created"] is True and body["status"] == "queued" and body["kind"] == kind
    assert body["queued_at"] and body["started_at"] is None
    (name, kwargs, task_id), = world["dispatcher"].calls
    assert name == task and task_id == body["task_id"]
    assert kwargs["user_id"] == world["users"]["alice"].id and kwargs["run_id"] == body["id"]
    # The row existed, queued with its task id, when the task was enqueued.
    assert world["dispatcher"].rows_at_dispatch == [("queued", body["task_id"])]
    if kind == "download_sync":
        assert kwargs["time_range"] == "3m" and body["params"] == {"time_range": "3m"}


@pytest.mark.parametrize("path", ["/library/sync", "/library/download-sync"])
def test_a_double_click_returns_the_same_run(world, path):
    first = world["client"].post(path, headers=world["headers"]["alice"])
    second = world["client"].post(path, headers=world["headers"]["alice"])

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"] and second.json()["created"] is False
    assert len(world["dispatcher"].calls) == 1


def test_a_running_run_is_returned_too_and_the_other_kind_is_independent(world):
    alice = world["users"]["alice"]
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]
    with Session(world["engine"]) as session:
        assert store.mark_running(session, run_id)

    again = world["client"].post("/library/sync", headers=world["headers"]["alice"])
    other = world["client"].post("/library/download-sync", headers=world["headers"]["alice"])

    assert again.status_code == 200 and again.json()["status"] == "running" and again.json()["id"] == run_id
    assert other.status_code == 202
    with Session(world["engine"]) as session:
        assert {r.kind for r in store.active_runs(session, alice.id)} == {"library_sync", "download_sync"}


def test_a_finished_run_does_not_block_a_new_one(world, events):
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]
    with Session(world["engine"]) as session:
        store.mark_running(session, run_id)
        store.finish_run(session, run_id, status="completed")

    resp = world["client"].post("/library/sync", headers=world["headers"]["alice"])

    assert resp.status_code == 202 and resp.json()["id"] != run_id


def test_one_active_run_per_user_and_kind_is_enforced_by_the_database(engine):
    user = _user(engine, "alice")
    with Session(engine) as session:
        store.create_run(session, user.id, "library_sync")
        with pytest.raises(IntegrityError):
            store.create_run(session, user.id, "library_sync", status="running")
        session.rollback()
        store.create_run(session, user.id, "download_sync")


def test_a_concurrent_start_that_loses_the_race_returns_the_winner(engine, events, alive, monkeypatch):
    user = _user(engine, "alice")
    with Session(engine) as session:
        winner = store.create_run(session, user.id, "library_sync")
    # The first read saw no active run (the other request had not committed yet).
    real_active = store.active_run
    reads = []

    def racing_active_run(session, user_id, kind):
        reads.append(kind)
        return None if len(reads) == 1 else real_active(session, user_id, kind)

    monkeypatch.setattr(store, "active_run", racing_active_run)
    calls = []
    with Session(engine) as session:
        run, created = library_jobs.start(session, user.id, "library_sync",
                                          dispatch=lambda *a: calls.append(a))

    assert created is False and run.id == winner.id and calls == []


def test_a_queue_that_cannot_be_reached_fails_the_run_with_503(world):
    world["dispatcher"].fail = True

    resp = world["client"].post("/library/sync", headers=world["headers"]["alice"])

    assert resp.status_code == 503 and resp.headers["Retry-After"]
    assert resp.json()["detail"]["error_code"] == "queue_unavailable"
    status = world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()
    assert status["status"] == "failed" and status["error_code"] == "queue_unavailable"
    # Not stuck: the next click can try again.
    world["dispatcher"].fail = False
    assert world["client"].post("/library/sync", headers=world["headers"]["alice"]).status_code == 202


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _factory(engine):
    return lambda: Session(engine)


def test_queued_running_completed(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        run = store.create_run(session, user.id, "library_sync", task_id="t1")

    reporter = library_jobs.JobReporter.claim(user.id, "library_sync", run.id, session_factory=_factory(engine))
    running = _run(engine, run.id)
    assert reporter is not None and running.status == "running"
    assert running.started_at is not None and running.heartbeat_at is not None

    assert reporter.finish("completed", result={"tracks_synced": 3})
    done = _run(engine, run.id)
    assert done.status == "completed" and done.completed_at is not None
    assert json.loads(done.result) == {"tracks_synced": 3} and done.phase is None
    assert [e["run"]["status"] for e in events] == ["running", "completed"]


def test_running_failed_keeps_the_readable_message_and_the_raw_detail(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        run = store.create_run(session, user.id, "download_sync")
    reporter = library_jobs.JobReporter.claim(user.id, "download_sync", run.id, session_factory=_factory(engine))

    reporter.finish("failed", error_code="job_failed", error_detail="boom at https://user:secret@host/x")

    failed = _run(engine, run.id)
    assert failed.status == "failed" and failed.error_code == "job_failed"
    assert failed.error_message == library_jobs.MESSAGES["job_failed"]
    assert "secret" not in failed.error_detail


def test_a_run_closed_as_stale_is_never_revived_by_a_late_worker(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        run_id = store.create_run(session, user.id, "library_sync").id
        store.finish_run(session, run_id, status="failed", error_code="worker_unavailable")

    assert library_jobs.JobReporter.claim(user.id, "library_sync", run_id, session_factory=_factory(engine)) is None
    assert _run(engine, run_id).status == "failed"


def test_a_finished_run_does_not_go_back(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        run = store.create_run(session, user.id, "library_sync")
    reporter = library_jobs.JobReporter.claim(user.id, "library_sync", run.id, session_factory=_factory(engine))
    reporter.finish("completed")

    assert reporter.finish("failed", error_code="job_failed") is False
    assert reporter.progress("saving", processed=1, total=2, force=True)  # due, but the write is refused
    assert reporter.cancelled and _run(engine, run.id).status == "completed"


def test_a_task_without_a_run_creates_one_unless_another_is_active(engine, events):
    user = _user(engine, "alice")
    reporter = library_jobs.JobReporter.claim(user.id, "library_sync", None, session_factory=_factory(engine))
    assert reporter is not None and _run(engine, reporter.run_id).status == "running"

    assert library_jobs.JobReporter.claim(user.id, "library_sync", None, session_factory=_factory(engine)) is None


# ---------------------------------------------------------------------------
# Progress: fields, throttling
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _claimed(engine, **kwargs):
    user = _user(engine, "alice")
    with Session(engine) as session:
        run = store.create_run(session, user.id, "library_sync")
    return library_jobs.JobReporter.claim(user.id, "library_sync", run.id, session_factory=_factory(engine), **kwargs)


def test_progress_fields_are_written_with_a_heartbeat(engine, events):
    reporter = _claimed(engine)
    reporter.set_providers(["one", "two"])
    before = _run(engine, reporter.run_id).heartbeat_at

    reporter.progress("saving", provider="one", processed=40, total=120, force=True)

    run = _run(engine, reporter.run_id)
    assert (run.phase, run.provider, run.processed, run.total) == ("saving", "one", 40, 120)
    assert run.heartbeat_at >= before
    entries = {e["provider"]: e for e in json.loads(run.providers)}
    assert entries["one"]["status"] == "running" and (entries["one"]["processed"], entries["one"]["total"]) == (40, 120)
    assert entries["two"]["status"] == "queued"
    public = events[-1]["run"]
    assert public["phase"] == "saving" and public["processed"] == 40 and public["total"] == 120
    assert [p["provider"] for p in public["providers"]] == ["one", "two"]


def test_progress_writes_are_throttled(engine, events):
    clock = _Clock()
    reporter = _claimed(engine, min_interval=2.0, every_items=100, clock=clock)

    assert reporter.progress("saving", provider="p", processed=1, total=1000)  # new phase: written
    assert not reporter.progress("saving", provider="p", processed=2, total=1000)
    assert not reporter.progress("saving", provider="p", processed=50, total=1000)
    clock.now += 2.0
    assert reporter.progress("saving", provider="p", processed=60, total=1000)  # interval elapsed
    assert reporter.progress("saving", provider="p", processed=160, total=1000)  # 100 items
    assert not reporter.progress("saving", provider="p", processed=170, total=1000)
    assert reporter.progress("fetching", provider="q", processed=0)  # phase or provider changed
    assert reporter.progress("fetching", provider="q", processed=5, total=5)  # the end of a phase

    assert reporter.writes == 5
    assert _run(engine, reporter.run_id).processed == 5
    assert len(events) == 1 + 5  # claim + one per write


def test_the_library_sync_task_reports_every_provider(engine, events, monkeypatch):
    from workers import tasks

    user = _user(engine, "alice")
    liked = [make_track(f"Song {i}", provider="one", provider_id=f"o{i}") for i in range(3)]

    class _Library(FakeMetadataProvider):
        auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

        def __init__(self, name, tracks=(), fail=None):
            super().__init__(name)
            self._liked, self._fail = list(tracks), fail

        def supports_user_library(self):
            return True

        def get_saved_tracks(self, session, limit=50, offset=0):
            if self._fail:
                raise self._fail
            return self._liked[offset:offset + limit]

    registry = FakeRegistry(metadata=[_Library("one", liked), _Library("two", fail=ConnectionError("Connection refused"))])
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: registry)
    monkeypatch.setattr(tasks, "own_store", lambda session, user_id: None)
    monkeypatch.setattr(tasks, "has_own_account", lambda store, provider: True)
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    started = []
    monkeypatch.setattr(tasks.routines, "start_routine", started.append)
    with Session(engine) as session:
        run = store.create_run(session, user.id, "library_sync")

    outcome = tasks.sync_saved_tracks_for_user.run(user_id=user.id, run_id=run.id)

    done = _run(engine, run.id)
    assert outcome["status"] == "done" and done.status == "completed"
    assert json.loads(done.result) == {"tracks_synced": 3}
    entries = {e["provider"]: e for e in json.loads(done.providers)}
    assert entries["one"]["status"] == "completed" and entries["one"]["count"] == 3
    assert (entries["one"]["processed"], entries["one"]["total"]) == (3, 3)
    assert entries["two"]["status"] == "failed" and entries["two"]["error_code"] == "metadata_provider_unreachable"
    phases = [(e["run"]["phase"], e["run"]["provider"]) for e in events if e["run"]["status"] == "running"]
    assert ("fetching", "one") in phases and ("saving", "one") in phases and ("fetching", "two") in phases
    # The likes just imported: the library is brought up to date, the matching follows it.
    assert started == ["sync_library_index"]
    with Session(engine) as session:
        assert len(session.exec(select(UserSavedTrack)).all()) == 3


def test_the_library_sync_fails_when_every_provider_failed(engine, events, monkeypatch):
    from workers import tasks

    user = _user(engine, "alice")

    class _Library(FakeMetadataProvider):
        auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

        def supports_user_library(self):
            return True

    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry(metadata=[_Library("one")]))
    monkeypatch.setattr(tasks, "own_store", lambda session, user_id: None)
    monkeypatch.setattr(tasks, "has_own_account", lambda store, provider: False)  # never connected
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks.routines, "start_routine", lambda key: True)
    with Session(engine) as session:
        run = store.create_run(session, user.id, "library_sync")

    tasks.sync_saved_tracks_for_user.run(user_id=user.id, run_id=run.id)

    failed = _run(engine, run.id)
    assert failed.status == "failed" and failed.error_code == "provider_not_connected"


def test_the_download_sync_task_counts_what_it_checked_and_queued(engine, events, monkeypatch):
    from db.models import TrackRecord
    from workers import tasks

    user = _user(engine, "alice")
    with Session(engine) as session:
        for i in range(4):
            record = TrackRecord(title=f"T{i}", artist="A", album="B",
                                 track_data=json.dumps({"title": f"T{i}", "artist": "A", "album": "B",
                                                        "duration_ms": 1}))
            session.add(record)
            session.flush()
            session.add(UserSavedTrack(user_id=user.id, track_id=record.internal_id))
        session.commit()
        run = store.create_run(session, user.id, "download_sync", params={"time_range": "all"})
    queued = []
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tasks, "get_config", lambda: ConfigManager(engine=engine))
    monkeypatch.setattr(downloads, "enqueue_track", lambda track, session, **kw: (
        queued.append(track.title), downloads.EnqueueResult(downloads.QUEUED, "t", "d"))[1])

    tasks.library_download_sync.run(user_id=user.id, time_range="all", run_id=run.id)

    done = _run(engine, run.id)
    assert done.status == "completed"
    assert json.loads(done.result) == {"total": 4, "checked": 4, "queued": 4, "errors": 0, "skipped_removed": 0}
    checking = [e["run"] for e in events if e["run"]["phase"] == "checking"]
    assert checking and checking[-1]["processed"] == 4 and checking[-1]["total"] == 4


def test_the_periodic_sync_goes_through_the_idempotent_start(engine, events, alive, monkeypatch):
    from workers import tasks

    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    with Session(engine) as session:
        busy = store.create_run(session, alice.id, "library_sync")
    calls = []
    monkeypatch.setattr(tasks, "engine", engine)
    _library_accounts(monkeypatch, {alice.id, bob.id})
    monkeypatch.setattr(library_jobs, "_dispatch", lambda name, kwargs, task_id: calls.append(kwargs))

    result = tasks.sync_all_users_saved_tracks.run()

    assert result == {"dispatched": 1, "users": 2, "skipped_not_connected": 0}
    assert [c["user_id"] for c in calls] == [bob.id]
    with Session(engine) as session:
        assert store.active_run(session, alice.id, "library_sync").id == busy.id


def _library_accounts(monkeypatch, connected: set[str]) -> None:
    """One provider with a user library; the users in ``connected`` have
    their own account on it."""
    from workers import tasks

    class _Library(FakeMetadataProvider):
        auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

        def supports_user_library(self):
            return True

    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry(metadata=[_Library("one")]))
    monkeypatch.setattr(tasks, "own_store", lambda session, user_id: user_id)
    monkeypatch.setattr(tasks, "has_own_account", lambda store, provider: store in connected)


def test_the_periodic_sync_skips_a_user_with_no_connected_account(engine, events, alive, monkeypatch):
    """Seen live: a user with no provider connected got a failed
    ``provider_not_connected`` run every six hours."""
    from workers import tasks

    alice, bob = _user(engine, "alice"), _user(engine, "bob")
    calls = []
    monkeypatch.setattr(tasks, "engine", engine)
    _library_accounts(monkeypatch, {alice.id})
    monkeypatch.setattr(library_jobs, "_dispatch", lambda name, kwargs, task_id: calls.append(kwargs))

    result = tasks.sync_all_users_saved_tracks.run()

    assert result == {"dispatched": 1, "users": 2, "skipped_not_connected": 1}
    assert [c["user_id"] for c in calls] == [alice.id]
    with Session(engine) as session:
        assert store.latest_run(session, bob.id, "library_sync") is None


# ---------------------------------------------------------------------------
# Stale runs
# ---------------------------------------------------------------------------


def test_a_queued_run_with_no_worker_becomes_worker_unavailable(world):
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]
    world["alive"]["value"] = False

    # Within the grace it is still waiting.
    assert world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()["status"] == "queued"
    _age(world["engine"], run_id, created=timedelta(seconds=library_jobs.QUEUED_NO_WORKER_SECONDS + 1))
    status = world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()

    assert status["status"] == "failed" and status["error_code"] == "worker_unavailable"
    assert status["error_message"] and status["completed_at"]
    assert world["events"][-1]["run"]["error_code"] == "worker_unavailable"
    assert world["events"][-1]["user_id"] == world["users"]["alice"].id


@pytest.mark.parametrize("liveness", [True, None])
def test_a_queued_run_waits_while_a_worker_is_alive_or_nothing_is_known(world, liveness):
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]
    world["alive"]["value"] = liveness
    _age(world["engine"], run_id, created=timedelta(minutes=30))

    assert world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()["status"] == "queued"


def test_a_queued_run_waiting_too_long_is_given_up_even_with_a_worker(world):
    run_id = world["client"].post("/library/download-sync", headers=world["headers"]["alice"]).json()["id"]
    _age(world["engine"], run_id, created=timedelta(seconds=library_jobs.QUEUED_MAX_SECONDS + 1))

    status = world["client"].get("/library/download-sync/status", headers=world["headers"]["alice"]).json()

    assert status["status"] == "failed" and status["error_code"] == "worker_unavailable"


def test_a_running_run_without_heartbeat_becomes_worker_lost(world):
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]
    with Session(world["engine"]) as session:
        store.mark_running(session, run_id)
    _age(world["engine"], run_id, heartbeat=timedelta(seconds=library_jobs.RUNNING_STALE_SECONDS - 30))
    assert world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()["status"] == "running"

    _age(world["engine"], run_id, heartbeat=timedelta(seconds=library_jobs.RUNNING_STALE_SECONDS + 1))
    status = world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()

    assert status["status"] == "failed" and status["error_code"] == "worker_lost"
    # A new click starts again instead of returning the lost run.
    assert world["client"].post("/library/sync", headers=world["headers"]["alice"]).status_code == 202


def test_a_worker_restart_fails_the_runs_it_left_running(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        running = store.create_run(session, user.id, "library_sync", status="running").id
        queued = store.create_run(session, user.id, "download_sync").id
        assert library_jobs.reap_orphaned_running(session) == 1

    assert _run(engine, running).error_code == "worker_lost"
    assert _run(engine, queued).status == "queued"  # still in the broker: the new worker takes it


def test_liveness_is_asked_only_for_a_queued_run_past_the_grace(engine, events):
    user = _user(engine, "alice")
    with Session(engine) as session:
        store.create_run(session, user.id, "library_sync")
        asked = []
        library_jobs.reap_stale(session, user_id=user.id, alive=lambda: asked.append(1) or False)
    assert asked == []


# ---------------------------------------------------------------------------
# Visibility: events, permissions
# ---------------------------------------------------------------------------


def test_job_events_reach_their_owner_only_admins_included(world):
    world["client"].post("/library/sync", headers=world["headers"]["bob"])
    world["alive"]["value"] = False
    run_id = world["client"].get("/library/sync/status", headers=world["headers"]["bob"]).json()["id"]
    _age(world["engine"], run_id, created=timedelta(hours=1))
    world["client"].get("/library/sync/status", headers=world["headers"]["bob"])
    event = world["events"][-1]
    users = world["users"]

    assert event["type"] == "library_job" and event["user_id"] == users["bob"].id
    assert "error_detail" not in event["run"]
    assert event_visible_to(event, users["bob"], lambda _id: False)
    assert not event_visible_to(event, users["alice"], lambda _id: True)
    assert not event_visible_to(event, users["root"], lambda _id: True)


@pytest.mark.parametrize("method, path", [
    ("POST", "/library/sync"),
    ("GET", "/library/sync/status"),
    ("POST", "/library/download-sync"),
    ("GET", "/library/download-sync/status"),
    ("GET", "/library/jobs"),
])
def test_a_guest_gets_401(world, method, path):
    assert world["client"].request(method, path).status_code == 401
    assert world["dispatcher"].calls == []


def test_a_user_sees_only_their_own_runs(world):
    world["client"].post("/library/sync", headers=world["headers"]["bob"])

    jobs = world["client"].get("/library/jobs", headers=world["headers"]["alice"]).json()
    mine = world["client"].post("/library/sync", headers=world["headers"]["alice"])

    assert jobs == {"library_sync": {"kind": "library_sync", "status": "never_run"},
                    "download_sync": {"kind": "download_sync", "status": "never_run"}}
    assert mine.status_code == 202  # bob's run does not block alice


def test_the_raw_error_is_for_admins_only(world):
    for name in ("alice", "root"):
        user = world["users"][name]
        with Session(world["engine"]) as session:
            run_id = store.create_run(session, user.id, "library_sync", status="running").id
            store.finish_run(session, run_id, status="failed", error_code="job_failed",
                             error_message="The job failed.", error_detail="Traceback: boom",
                             providers=[{"provider": "one", "status": "failed", "error_code": "x",
                                         "error_message": "X.", "error_detail": "raw one"}])

    user_view = world["client"].get("/library/sync/status", headers=world["headers"]["alice"]).json()
    admin_view = world["client"].get("/library/jobs", headers=world["headers"]["root"]).json()["library_sync"]

    assert "error_detail" not in user_view and "error_detail" not in user_view["providers"][0]
    assert admin_view["error_detail"] == "Traceback: boom"
    assert admin_view["providers"][0]["error_detail"] == "raw one"


def test_deleting_the_library_data_keeps_an_active_run(world):
    run_id = world["client"].post("/library/sync", headers=world["headers"]["alice"]).json()["id"]

    assert world["client"].delete("/library/saved-tracks", headers=world["headers"]["alice"]).status_code == 200

    assert _run(world["engine"], run_id) is not None
