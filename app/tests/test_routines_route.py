"""``routes/system/routines.py`` — GET/PATCH/POST /system/routines*.

Segue lo stile di ``test_config_route.py``/``test_history_permissions.py``:
le route sono funzioni Python ordinarie, richiamate direttamente con
dipendenze isolate (engine SQLite in-memory dedicato), non tramite
TestClient/app reale. Il confine di permesso admin-only (401/403 reali,
attraverso la catena di dipendenze FastAPI) è invece coperto da
``test_routines_api_permissions.py``, che monta il router su un'app FastAPI
minimale — qui non serve rifarlo per ogni endpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from services.config_manager import ConfigManager
from core.routines import ROUTINES
from db.models import User
from db.routine_run_store import claim_run, finish_run, queue_run
from routes.system import routines as routines_route
from workers import scheduler

from tests.conftest import FakeRegistry


@pytest.fixture(autouse=True)
def _route_dispatch_through_test_engine(monkeypatch, engine):
    # run_routine_now instrada su workers.scheduler.dispatch_routine, che
    # apre la propria Session sul suo `engine` di modulo — deve essere lo
    # stesso DB di questo test, non quello di produzione.
    monkeypatch.setattr(scheduler, "engine", engine)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


@pytest.fixture
def registry():
    return FakeRegistry()


def start_run(session, key):
    """A run a worker has started (queued, then claimed)."""
    run = queue_run(session, key)
    return claim_run(session, run.id)


def _admin(session) -> User:
    user = User(username="admin", password_hash="x", role="admin")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# GET /system/routines
# ---------------------------------------------------------------------------


def test_list_routines_reports_every_declared_routine_with_defaults(config, registry, session):
    admin = _admin(session)

    data = routines_route.list_routines(admin, config, registry, session)

    keys = {r["key"] for r in data["routines"]}
    assert keys == {r.key for r in ROUTINES}
    for entry, routine in zip(
        sorted(data["routines"], key=lambda r: r["key"]),
        sorted(ROUTINES, key=lambda r: r.key),
    ):
        assert entry["task"] == routine.task_name
        assert entry["enabled"] == routine.default_enabled
        assert entry["interval_minutes"] == routine.default_interval_minutes
        assert entry["min_interval_minutes"] == routine.min_interval_minutes
        assert entry["last_run"] is None
        assert entry["next_run_at"] is None
        assert entry["running"] is False
        assert entry["queued"] is False


def test_list_routines_reports_last_run_and_next_run_at(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]

    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    run = start_run(session, routine.key)
    run.started_at = started
    session.add(run)
    session.commit()
    finish_run(session, run.id, status="ok", detail="dispatched 2")

    data = routines_route.list_routines(admin, config, registry, session)
    entry = next(r for r in data["routines"] if r["key"] == routine.key)

    assert entry["last_run"]["status"] == "ok"
    # Not an object: kept as text.
    assert entry["last_run"]["result"] is None
    assert entry["last_run"]["detail"] == "dispatched 2"
    assert entry["running"] is False
    assert entry["next_run_at"] is not None


def _finished_run(session, key, *, detail, seconds=2.5, status="ok"):
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    run = start_run(session, key)
    run.queued_at = started - timedelta(seconds=40)
    run.started_at = started
    session.add(run)
    session.commit()
    finish_run(session, run.id, status=status, detail=detail)
    run = session.get(type(run), run.id)
    run.finished_at = started + timedelta(seconds=seconds)
    session.add(run)
    session.commit()


def _entry(data, key):
    return next(r for r in data["routines"] if r["key"] == key)


def test_a_run_result_is_data_and_the_run_has_a_duration(config, registry, session):
    admin = _admin(session)
    _finished_run(session, "match_likes", detail='{"checked": 1652, "matched": 33}', seconds=1.1)

    run = _entry(routines_route.list_routines(admin, config, registry, session), "match_likes")["last_run"]

    assert run["result"] == {"checked": 1652, "matched": 33}
    assert run["detail"] is None
    # The execution only; the 40 s it waited in the queue apart.
    assert run["duration_seconds"] == pytest.approx(1.1)
    assert run["wait_seconds"] == pytest.approx(40)


def test_a_result_stored_before_as_python_repr_is_read_as_data(config, registry, session):
    admin = _admin(session)
    _finished_run(session, "reconcile_pending_likes", detail="{'status': 'swept', 'count': 140}")

    run = _entry(routines_route.list_routines(admin, config, registry, session), "reconcile_pending_likes")["last_run"]

    assert run["result"] == {"status": "swept", "count": 140}
    assert run["detail"] is None


def test_a_failed_run_is_never_parsed_as_a_result(config, registry, session):
    admin = _admin(session)
    _finished_run(session, "match_likes", detail="{'looks': 'like data'}", status="error")

    run = _entry(routines_route.list_routines(admin, config, registry, session), "match_likes")["last_run"]

    assert run["result"] is None
    assert run["status"] == "error"


def test_the_times_carry_their_offset(config, registry, session):
    admin = _admin(session)
    _finished_run(session, "match_likes", detail="{}")

    entry = _entry(routines_route.list_routines(admin, config, registry, session), "match_likes")

    for value in (entry["last_run"]["queued_at"], entry["last_run"]["started_at"], entry["last_run"]["finished_at"],
                  entry["next_run_at"]):
        assert value.utcoffset() == timedelta(0)


def test_the_work_a_routine_set_going_is_counted(config, registry, session):
    from db.models import LibraryJobRun

    admin = _admin(session)
    other = User(username="other", password_hash="x")
    session.add(other)
    session.commit()
    session.add(LibraryJobRun(user_id=admin.id, kind="library_sync", status="running"))
    session.add(LibraryJobRun(user_id=other.id, kind="library_sync", status="queued"))
    session.add(LibraryJobRun(user_id=other.id, kind="download_sync", status="running"))
    session.commit()

    data = routines_route.list_routines(admin, config, registry, session)

    assert _entry(data, "sync_saved_tracks")["active_work"] == 2
    assert _entry(data, "sweep_delegations")["active_work"] == 0
    assert _entry(data, "match_likes")["active_work"] is None


def test_list_routines_reports_running_true_for_a_fresh_running_row(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    start_run(session, routine.key)

    data = routines_route.list_routines(admin, config, registry, session)
    entry = next(r for r in data["routines"] if r["key"] == routine.key)

    assert entry["running"] is True
    assert entry["queued"] is False


def test_a_run_waiting_in_the_queue_is_queued_not_running(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    queue_run(session, routine.key)

    entry = _entry(routines_route.list_routines(admin, config, registry, session), routine.key)

    assert entry["queued"] is True and entry["running"] is False
    assert entry["last_run"]["status"] == "queued"
    assert entry["last_run"]["started_at"] is None
    assert entry["last_run"]["wait_seconds"] is None and entry["last_run"]["duration_seconds"] is None
    # Never started: due now, not "next in an interval from the dispatch".
    assert entry["next_run_at"] is None


# ---------------------------------------------------------------------------
# PATCH /system/routines/{key}
# ---------------------------------------------------------------------------


def test_patch_unknown_routine_is_404(config, registry, session):
    admin = _admin(session)
    with pytest.raises(HTTPException) as exc_info:
        routines_route.patch_routine(
            "not-a-routine", routines_route.RoutinePatch(enabled=False), admin, config, registry, session,
        )
    assert exc_info.value.status_code == 404


def test_patch_with_no_fields_is_400(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    with pytest.raises(HTTPException) as exc_info:
        routines_route.patch_routine(routine.key, routines_route.RoutinePatch(), admin, config, registry, session)
    assert exc_info.value.status_code == 400


def test_patch_interval_below_minimum_is_rejected(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    below_minimum = max(routine.min_interval_minutes - 1, 0)

    with pytest.raises(HTTPException) as exc_info:
        routines_route.patch_routine(
            routine.key, routines_route.RoutinePatch(interval_minutes=below_minimum), admin, config, registry, session,
        )
    assert exc_info.value.status_code == 400

    # Nulla scritto: il valore resta il default dichiarato.
    data = routines_route.list_routines(admin, config, registry, session)
    entry = next(r for r in data["routines"] if r["key"] == routine.key)
    assert entry["interval_minutes"] == routine.default_interval_minutes


def test_patch_valid_update_is_reflected_immediately(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    new_interval = routine.min_interval_minutes + 1

    routines_route.patch_routine(
        routine.key,
        routines_route.RoutinePatch(enabled=False, interval_minutes=new_interval),
        admin, config, registry, session,
    )

    data = routines_route.list_routines(admin, config, registry, session)
    entry = next(r for r in data["routines"] if r["key"] == routine.key)
    assert entry["enabled"] is False
    assert entry["interval_minutes"] == new_interval


# ---------------------------------------------------------------------------
# POST /system/routines/{key}/run
# ---------------------------------------------------------------------------


def test_run_now_unknown_routine_is_404(config, registry, session):
    admin = _admin(session)
    with pytest.raises(HTTPException) as exc_info:
        routines_route.run_routine_now("not-a-routine", admin)
    assert exc_info.value.status_code == 404


def test_run_now_already_queued_is_409(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    queue_run(session, routine.key)

    with pytest.raises(HTTPException) as exc_info:
        routines_route.run_routine_now(routine.key, admin)
    assert exc_info.value.status_code == 409


def test_run_now_already_running_is_409(config, registry, session):
    admin = _admin(session)
    routine = ROUTINES[0]
    start_run(session, routine.key)

    with pytest.raises(HTTPException) as exc_info:
        routines_route.run_routine_now(routine.key, admin)
    assert exc_info.value.status_code == 409


def test_run_now_dispatches_and_returns_a_run_id(config, registry, session, monkeypatch):
    admin = _admin(session)
    routine = ROUTINES[0]

    captured = {}
    monkeypatch.setattr(
        scheduler.run_routine, "apply_async",
        lambda kwargs=None, **opts: captured.update(kwargs),
    )

    result = routines_route.run_routine_now(routine.key, admin)

    assert "run_id" in result
    assert captured["routine_key"] == routine.key
    assert captured["run_id"] == result["run_id"]

    # La riga c'e' gia' ('queued') PRIMA che apply_async venga chiamata (fix
    # della race), senza periodo ("Esegui ora"): list_routines la vede
    # subito in coda, non in corso.
    assert captured["period_minutes"] is None
    data = routines_route.list_routines(admin, config, registry, session)
    entry = next(r for r in data["routines"] if r["key"] == routine.key)
    assert entry["queued"] is True and entry["running"] is False


# ---------------------------------------------------------------------------
# scheduler_stale: una prossima esecuzione scaduta da oltre il margine vuol
# dire che worker o beat non girano.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def test_scheduler_is_not_stale_when_the_next_run_is_in_the_future():
    assert routines_route.is_scheduler_stale(True, _NOW + timedelta(minutes=10), _NOW) is False


def test_scheduler_is_stale_when_the_next_run_is_past_the_grace_period():
    late = _NOW - routines_route.SCHEDULER_GRACE - timedelta(seconds=1)
    assert routines_route.is_scheduler_stale(True, late, _NOW) is True


def test_exactly_at_the_grace_period_is_not_yet_stale():
    assert routines_route.is_scheduler_stale(True, _NOW - routines_route.SCHEDULER_GRACE, _NOW) is False


def test_a_routine_that_never_ran_says_nothing_about_the_scheduler():
    assert routines_route.is_scheduler_stale(True, None, _NOW) is False


def test_a_disabled_routine_is_never_stale():
    assert routines_route.is_scheduler_stale(False, _NOW - timedelta(days=1), _NOW) is False


def test_list_routines_flags_a_routine_whose_next_run_is_long_overdue(config, registry, session):
    admin = _admin(session)
    routine = next(r for r in ROUTINES if r.default_enabled)
    run = start_run(session, routine.key)
    run.started_at = datetime.now(timezone.utc) - timedelta(minutes=routine.default_interval_minutes + 60)
    session.add(run)
    session.commit()
    finish_run(session, run.id, status="ok", detail="")

    data = routines_route.list_routines(admin, config, registry, session)

    entry = next(r for r in data["routines"] if r["key"] == routine.key)
    assert entry["scheduler_stale"] is True
    assert data["scheduler_stale"] is True


def test_list_routines_is_not_stale_on_a_fresh_install(config, registry, session):
    data = routines_route.list_routines(_admin(session), config, registry, session)

    assert data["scheduler_stale"] is False
    assert all(r["scheduler_stale"] is False for r in data["routines"])
