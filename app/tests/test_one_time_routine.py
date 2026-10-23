"""A routine that is a job done once (``RoutineDef.done_when``): the tick runs it until its flag is set and then
leaves it alone, the page shows it as done, "Run now" still runs it, and clearing the flag re-arms it.

Data-driven: the scheduler knows nothing of any particular routine; the declaration of
``import_existing_library`` is checked at the end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from core.routines import LIBRARY_SCANNED, ROUTINES, RoutineDef, get_routine, is_done
from db.models import User
from db.routine_run_store import claim_run, finish_run, queue_run
from db.settings_store import SettingsRepository
from routes.system import routines as routines_route
from services.config_manager import ConfigManager
from tests.conftest import FakeRegistry
from workers import scheduler

FLAG = "some_job_finished"
ONCE = RoutineDef(key="once", task_name="tests.fake.once", default_interval_minutes=5, min_interval_minutes=1, done_when=FLAG)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, engine):
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr(scheduler, "ROUTINES", (ONCE,))
    monkeypatch.setattr(routines_route, "ROUTINES", (ONCE,))
    monkeypatch.setattr(routines_route, "get_routine", lambda key: ONCE if key == ONCE.key else None)


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(scheduler.run_routine, "apply_async", lambda kwargs=None, **opts: seen.append(kwargs["routine_key"]))
    return seen


def set_flag(engine, value) -> None:
    with Session(engine) as session:
        SettingsRepository(session).set_system(FLAG, value)


def a_finished_run(engine, minutes_ago=30, detail='{"status": "ok", "state": "imported", "indexed": 7}'):
    with Session(engine) as session:
        run = claim_run(session, queue_run(session, ONCE.key).id)
        run.started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        session.add(run)
        session.commit()
        finish_run(session, run.id, status="ok", detail=detail)


def test_the_flag_is_what_makes_a_routine_done():
    assert is_done(ONCE, {FLAG: True}) is True
    for flag in ({}, {FLAG: False}, {FLAG: None}, {FLAG: "yes"}, {"other": True}):
        assert is_done(ONCE, flag) is False
    periodic = RoutineDef(key="p", task_name="t", default_interval_minutes=5)
    assert is_done(periodic, {FLAG: True, "": True}) is False, "a routine with no completion flag is never done"


def test_the_tick_runs_it_until_the_flag_is_set_and_then_leaves_it_alone(engine, calls):
    scheduler.tick()
    assert calls == ["once"], "not done: due like any routine"

    a_finished_run(engine)  # long past its 5 minutes
    set_flag(engine, True)
    calls.clear()

    assert scheduler.tick() == {"dispatched": []}
    assert calls == []


def test_clearing_the_flag_re_arms_it(engine, calls):
    a_finished_run(engine)
    set_flag(engine, True)
    scheduler.tick()
    assert calls == []

    set_flag(engine, False)
    scheduler.tick()

    assert calls == ["once"]


def test_run_now_still_runs_a_routine_that_is_done(engine, calls):
    a_finished_run(engine)
    set_flag(engine, True)
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        session.refresh(admin)

    answer = routines_route.run_routine_now("once", admin)

    assert calls == ["once"] and answer["run_id"]


def test_the_page_shows_a_finished_job_as_done_with_its_last_result_and_no_next_run(engine):
    a_finished_run(engine, minutes_ago=3)
    set_flag(engine, True)
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        session.refresh(admin)
        entry = routines_route.list_routines(admin, ConfigManager(engine=engine), FakeRegistry(), session)["routines"][0]

    assert entry["done"] is True
    assert entry["next_run_at"] is None and entry["scheduler_stale"] is False
    assert entry["last_run"]["status"] == "ok" and entry["last_run"]["result"]["indexed"] == 7
    assert entry["last_run"]["finished_at"] is not None

    set_flag(engine, False)
    with Session(engine) as session:
        admin = session.exec(select(User)).first()
        again = routines_route.list_routines(admin, ConfigManager(engine=engine), FakeRegistry(), session)["routines"][0]
    assert again["done"] is False and again["next_run_at"] is not None


def test_only_the_library_import_is_declared_a_one_time_job_and_by_the_flag_the_scan_sets():
    once = [r for r in ROUTINES if r.done_when]

    assert [(r.key, r.done_when) for r in once] == [("import_existing_library", LIBRARY_SCANNED)]
    assert get_routine("import_existing_library").done_when == "library_scanned"

