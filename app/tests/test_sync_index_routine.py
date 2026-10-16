"""
The sync providers' libraries are indexed by a routine of their own,
``sync_library_index``, and the like matching follows it.

Seen live on 3 Oct: the music server was unreachable (DNS) from 09:06 to
12:06. The indexing ran inside the like matching, behind a try/except that
only logged a warning: every hourly run was recorded ``ok`` with
``{"checked": 1019, "matched": 0}`` and the failure never reached the
routines page. The first full indexing (3,749 → 7,675 records in 27 s) had no
history or progress of its own either.

Here, on the real scheduler (``workers/scheduler.py``), the real tasks
(``workers/tasks.py``) and the real routines route, against an isolated
database: the messages the host sends are recorded (``sent_tasks``, see
conftest) and executed one by one, in order, by name.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

import db.engine as db_engine
from core import settings_catalog
from core.identifiers import sync_id_type
from musizerr_sdk.models import LibraryItem
from core.routines import SYNC_LIBRARY_INDEX, enabled_setting_key, get_routine, interval_setting_key
from db import routine_run_store as runs
from db.models import RoutineRun, TrackIdentifierRecord, User
from db.settings_store import SettingsRepository
from routes.system import routines as routines_route
from services import routines as routine_service
from services.config_manager import ConfigManager
from workers import scheduler, tasks
from workers.celery_app import QUEUE_JOBS, QUEUE_MAINTENANCE, queue_of

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider

APP = Path(__file__).resolve().parents[1]


def _items(prefix: str, count: int) -> list[LibraryItem]:
    return [
        LibraryItem(sync_id=f"{prefix}-{i}", path=f"{prefix}/{i}.flac", title=f"{prefix} song {i}",
                    artist=f"{prefix} band", album=f"{prefix} record", duration_ms=180_000 + i * 1000, year=2020)
        for i in range(count)
    ]


class _Unreachable(FakeSyncProvider):
    def list_library(self, session):
        raise OSError("[Errno -3] Temporary failure in name resolution")


@pytest.fixture
def engine(monkeypatch):
    eng = new_engine()
    for module in (scheduler, tasks):
        monkeypatch.setattr(module, "engine", eng)
    monkeypatch.setattr(db_engine, "engine", eng)  # RunProgress opens its own sessions there
    monkeypatch.setattr(scheduler, "worker_alive", lambda queue: True)
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks, "shared_store", lambda session: None)
    return eng


@pytest.fixture
def host(engine, monkeypatch, sent_tasks):
    """The registry the tasks see, and the worker side: ``drain`` executes
    the recorded messages in order, as the workers would."""
    state = SimpleNamespace(registry=FakeRegistry(), executed=[])
    monkeypatch.setattr(tasks, "get_registry", lambda: state.registry)
    # The two routines of the chain (the others would be due as well).
    monkeypatch.setattr(scheduler, "ROUTINES", (get_routine(SYNC_LIBRARY_INDEX), get_routine("match_likes")))

    handlers = {
        "workers.routines.run": lambda kwargs: scheduler.run_routine(**kwargs),
        "workers.routines.start": lambda kwargs: scheduler.start(**kwargs),
    }

    def drain() -> list[tuple[str, str]]:
        done = []
        while sent_tasks:
            name, kwargs, options = sent_tasks.pop(0)
            label = kwargs.get("routine_key", "")
            done.append((name, label, options.get("queue")))
            handlers[name](kwargs)
        state.executed.extend(done)
        return done

    state.drain = drain
    state.sent = sent_tasks
    return state


def _runs(engine, key: str) -> list[RoutineRun]:
    with Session(engine) as session:
        return list(session.exec(select(RoutineRun).where(RoutineRun.routine_key == key)
                                 .order_by(RoutineRun.queued_at)).all())


def _routines_page(engine) -> dict[str, dict]:
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        page = routines_route.list_routines(admin, ConfigManager(engine=engine), FakeRegistry(), session)
        session.delete(admin)
        session.commit()
    return {entry["key"]: entry for entry in page["routines"]}


def _sync_ids(engine, provider: str) -> set[str]:
    with Session(engine) as session:
        return set(session.exec(select(TrackIdentifierRecord.value)
                                .where(TrackIdentifierRecord.id_type == sync_id_type(provider))).all())


# ---------------------------------------------------------------------------
# The routine
# ---------------------------------------------------------------------------


def test_the_index_is_a_routine_with_its_own_period_switch_and_queue():
    routine = get_routine(SYNC_LIBRARY_INDEX)

    assert routine is not None and routine.task_name == "workers.tasks.sync_library_index"
    assert (routine.default_interval_minutes, routine.min_interval_minutes) == (60, 15)
    assert routine.default_enabled and routine.reports_progress
    # A large library takes minutes: never on the worker of the tick.
    assert queue_of(routine.task_name) == QUEUE_JOBS
    assert queue_of("workers.routines.tick") == QUEUE_MAINTENANCE
    keys = {s.key for s in settings_catalog.CATALOG}
    assert {enabled_setting_key(routine.key), interval_setting_key(routine.key)} <= keys
    # The like matching follows it, and no longer indexes itself.
    assert get_routine("match_likes").follows == SYNC_LIBRARY_INDEX


def test_a_scheduled_index_runs_on_the_jobs_queue_once_per_period(engine, host):
    sync = FakeSyncProvider("music", library=_items("a", 3))
    host.registry = FakeRegistry(syncs=[sync])

    assert scheduler.tick()["dispatched"] == [SYNC_LIBRARY_INDEX]  # the matching waits for it
    assert host.drain()[0] == ("workers.routines.run", SYNC_LIBRARY_INDEX, QUEUE_JOBS)
    assert scheduler.tick()["dispatched"] == []  # neither is due again

    # A second message for the same period (a backlog): skipped, nothing listed twice.
    calls = []
    sync.list_library = lambda session: calls.append(1) or _items("a", 3)
    with Session(engine) as session:
        late = runs.queue_run(session, SYNC_LIBRARY_INDEX)
    scheduler.run_routine(routine_key=SYNC_LIBRARY_INDEX, run_id=late.id, period_minutes=60)

    assert calls == []
    assert [run.status for run in _runs(engine, SYNC_LIBRARY_INDEX)] == ["ok", "skipped"]


def test_the_switch_turns_the_scheduled_index_off(engine, host):
    with Session(engine) as session:
        SettingsRepository(session).set_system(enabled_setting_key(SYNC_LIBRARY_INDEX), False)

    # Off: the matching runs on its own period.
    assert scheduler.tick()["dispatched"] == ["match_likes"]


def test_the_index_result_is_recorded_per_provider(engine, host):
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("music", library=_items("a", 3)),
                                        FakeSyncProvider("other", library=_items("b", 2))])

    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    last = _routines_page(engine)[SYNC_LIBRARY_INDEX]["last_run"]
    assert last["status"] == "ok" and last["error_code"] is None
    result = last["result"]
    assert [(p["sync_provider"], p["status"], p["listed"], p["created"]) for p in result["providers"]] == [
        ("music", "done", 3, 3), ("other", "done", 2, 2)]
    assert (result["listed"], result["created"], result["attached"], result["aliased"],
            result["stale_dropped"], result["gone"]) == (5, 5, 0, 0, 0, 0)
    assert _sync_ids(engine, "music") == {"a-0", "a-1", "a-2"}


# ---------------------------------------------------------------------------
# A failing sync provider is a failed run, never "ok" with a hidden warning
# ---------------------------------------------------------------------------


def test_an_unreachable_sync_provider_fails_the_run_with_a_stable_error_code(engine, host):
    host.registry = FakeRegistry(syncs=[_Unreachable("music"), FakeSyncProvider("other", library=_items("b", 2))])

    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    page = _routines_page(engine)
    last = page[SYNC_LIBRARY_INDEX]["last_run"]
    assert last["status"] == "error"
    assert last["error_code"] == "sync_provider_unreachable"
    assert "name resolution" in last["error_detail"]
    music, other = last["result"]["providers"]
    assert (music["sync_provider"], music["status"], music["error_code"]) == ("music", "error",
                                                                              "sync_provider_unreachable")
    # The provider that answered was indexed all the same.
    assert (other["status"], other["created"]) == ("done", 2)
    assert last["result"]["failed"] == ["music"]

    # The matching still ran, on what the database knows, and says so.
    match = page["match_likes"]["last_run"]
    assert match["status"] == "ok"
    assert match["result"]["library_index"]["status"] == "error"
    assert match["result"]["library_index"]["error_code"] == "sync_provider_unreachable"


def test_a_partial_listing_fails_the_run_and_drops_nothing(engine, host):
    sync = FakeSyncProvider("music", library=_items("a", 10))
    host.registry = FakeRegistry(syncs=[sync])
    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    sync._library = _items("a", 2)
    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    last = _routines_page(engine)[SYNC_LIBRARY_INDEX]["last_run"]
    assert last["status"] == "error" and last["error_code"] == "sync_provider_partial_listing"
    assert last["result"]["providers"][0]["gone"] == 8
    assert len(_sync_ids(engine, "music")) == 10


def test_an_empty_listing_fails_the_run(engine, host):
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("music", library=[])])

    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    assert _routines_page(engine)[SYNC_LIBRARY_INDEX]["last_run"]["error_code"] == "sync_provider_empty_listing"


def test_a_routine_that_returns_its_failure_is_a_failed_run(engine, monkeypatch):
    """Any routine: a result ``{"status": "error", ...}`` is an error run."""
    from core.routines import RoutineDef

    failing = RoutineDef(key="failing", task_name="tests.fake.failing", default_interval_minutes=60)
    monkeypatch.setattr(scheduler, "get_routine", lambda key: failing)
    monkeypatch.setattr(scheduler, "celery", SimpleNamespace(tasks={
        failing.task_name: SimpleNamespace(run=lambda: {"status": "error", "detail": "connection refused"}),
    }))
    with Session(engine) as session:
        run = runs.queue_run(session, failing.key)

    assert scheduler.run_routine(routine_key=failing.key, run_id=run.id)["status"] == "error"
    with Session(engine) as session:
        stored = session.get(RoutineRun, run.id)
        assert stored.status == "error"
        assert routine_service.run_error(stored)["error_code"] == "routine_unreachable"


# ---------------------------------------------------------------------------
# The matching follows the index
# ---------------------------------------------------------------------------


def test_the_matching_runs_after_a_successful_index(engine, host):
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("music", library=_items("a", 3))])

    assert scheduler.tick()["dispatched"] == [SYNC_LIBRARY_INDEX]
    executed = host.drain()

    assert [(name, key) for name, key, _ in executed] == [
        ("workers.routines.run", SYNC_LIBRARY_INDEX), ("workers.routines.run", "match_likes")]
    assert executed[1][2] == QUEUE_MAINTENANCE
    index, = _runs(engine, SYNC_LIBRARY_INDEX)
    match, = _runs(engine, "match_likes")
    assert match.started_at >= index.finished_at
    result = json.loads(match.detail)
    assert result["library_index"]["status"] == "ok" and result["checked"] == 0


def test_the_tick_does_not_start_the_matching_while_the_index_runs(engine, host):
    with Session(engine) as session:
        runs.claim_run(session, runs.queue_run(session, SYNC_LIBRARY_INDEX).id)

    assert scheduler.tick()["dispatched"] == []


def test_an_index_queued_with_no_worker_to_start_it_does_not_hold_the_matching(engine, host, monkeypatch):
    monkeypatch.setattr(scheduler, "worker_alive", lambda queue: queue != QUEUE_JOBS)
    with Session(engine) as session:
        runs.queue_run(session, SYNC_LIBRARY_INDEX)

    assert scheduler.tick()["dispatched"] == ["match_likes"]


def test_a_disabled_matching_does_not_follow(engine, host):
    with Session(engine) as session:
        SettingsRepository(session).set_system(enabled_setting_key("match_likes"), False)

    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    executed = host.drain()

    assert [key for _, key, _ in executed] == [SYNC_LIBRARY_INDEX]


# ---------------------------------------------------------------------------
# A library scan and a likes sync start the index, then the matching
# ---------------------------------------------------------------------------


def test_a_trigger_starts_the_index_then_the_matching(engine, host):
    """``services.routines.start_routine`` is what the library scan and the
    likes sync call (``tests/test_library_scan.py``, ``tests/test_library_jobs.py``)."""
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("music", library=_items("a", 2))])

    assert routine_service.start_routine(SYNC_LIBRARY_INDEX) is True
    executed = host.drain()

    assert [(name, key) for name, key, _ in executed] == [
        ("workers.routines.start", SYNC_LIBRARY_INDEX),
        ("workers.routines.run", SYNC_LIBRARY_INDEX),
        ("workers.routines.run", "match_likes"),
    ]
    assert [r.status for r in _runs(engine, SYNC_LIBRARY_INDEX)] == ["ok"]
    assert [r.status for r in _runs(engine, "match_likes")] == ["ok"]


def test_a_trigger_while_the_index_runs_starts_nothing_more(engine, host):
    with Session(engine) as session:
        running = runs.claim_run(session, runs.queue_run(session, SYNC_LIBRARY_INDEX).id)

    routine_service.start_routine(SYNC_LIBRARY_INDEX)
    executed = host.drain()

    assert [name for name, _, _ in executed] == ["workers.routines.start"]
    assert [r.id for r in _runs(engine, SYNC_LIBRARY_INDEX)] == [running.id]


def test_the_likes_sync_task_starts_the_index(engine, host, monkeypatch):
    """The real likes sync task, to its end: the message it sends."""
    from db import library_job_store as job_store

    with Session(engine) as session:
        user = User(username="alice", password_hash="x", role="user")
        session.add(user)
        session.commit()
        run = job_store.create_run(session, user.id, job_store.KIND_LIBRARY_SYNC)
        user_id, run_id = user.id, run.id
    monkeypatch.setattr(tasks, "_job_session", lambda: Session(engine))
    monkeypatch.setattr(tasks.library_jobs, "_publish", lambda session, run_id: None)

    tasks.sync_saved_tracks_for_user.run(user_id=user_id, run_id=run_id)

    assert [(name, kwargs) for name, kwargs, _ in host.sent] == [
        ("workers.routines.start", {"routine_key": SYNC_LIBRARY_INDEX})]


# ---------------------------------------------------------------------------
# Progress of a long run
# ---------------------------------------------------------------------------


def test_a_long_index_reports_its_progress_on_the_routines_page(engine, host, monkeypatch):
    host.registry = FakeRegistry(syncs=[FakeSyncProvider("music", library=_items("a", 250))])
    seen: list[dict] = []
    reported: list[tuple] = []

    class _Watched(routine_service.RunProgress):
        def __init__(self, run_id, **kwargs):
            super().__init__(run_id, min_interval=0)

        def __call__(self, phase, **fields):
            reported.append((phase, fields.get("processed"), fields.get("total")))
            written = super().__call__(phase, **fields)
            if written and phase == "indexing" and fields.get("processed") == 100:
                seen.append(_routines_page(engine)[SYNC_LIBRARY_INDEX])
            return written

    monkeypatch.setattr(scheduler, "RunProgress", _Watched)
    scheduler.dispatch_routine(get_routine(SYNC_LIBRARY_INDEX))
    host.drain()

    assert [phase for phase, *_ in reported][0] == "listing"
    assert ("indexing", 100, 250) in reported and ("indexing", 250, 250) in reported
    assert reported[-1][0] == "pruning"
    entry, = seen
    assert entry["running"] is True
    progress = entry["last_run"]["progress"]
    assert (progress["phase"], progress["provider"], progress["processed"], progress["total"]) == (
        "indexing", "music", 100, 250)
    assert progress["heartbeat_at"] is not None
    # Finished: no progress shown any more.
    assert _routines_page(engine)[SYNC_LIBRARY_INDEX]["last_run"]["progress"] is None


def test_progress_is_throttled_and_stops_on_a_run_closed_meanwhile(engine):
    clock = SimpleNamespace(now=0.0)
    with Session(engine) as session:
        run = runs.claim_run(session, runs.queue_run(session, SYNC_LIBRARY_INDEX).id)
    progress = routine_service.RunProgress(run.id, session_factory=lambda: Session(engine),
                                           min_interval=2.0, every_items=250, clock=lambda: clock.now)

    assert progress("indexing", provider="music", processed=0, total=1000) is True
    assert progress("indexing", provider="music", processed=100, total=1000) is False
    clock.now = 3.0
    assert progress("indexing", provider="music", processed=200, total=1000) is True
    with Session(engine) as session:
        runs.finish_run(session, run.id, status="error", detail="stale")
    assert progress("pruning", provider="music") is False and progress.cancelled


# ---------------------------------------------------------------------------
# Reads never index
# ---------------------------------------------------------------------------


_INDEXERS = {"index_libraries", "index_library", "index_track"}


def test_only_worker_jobs_call_the_indexer():
    """The sync presence is written by jobs; nothing in a route or another
    service (a read) calls the indexer. ``tests/test_identity_stays_clean.py``
    asserts that reads leave the database as it was."""
    callers = set()
    for package in ("routes", "services", "workers"):
        for path in (APP / package).rglob("*.py"):
            if path.name == "sync_index.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if name in _INDEXERS:
                        callers.add(str(path.relative_to(APP)))
    assert callers == {"workers/tasks.py"}
