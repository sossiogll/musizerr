"""
A routine run waiting in the queue is QUEUED, not running, and never runs
twice for the same period.

Seen live (2 Oct): with one worker sharing the queue with downloads of up to
~40 s each, a routine run dispatched at 15:18 started at 18:05. Its row said
"running" since 15:18 (``started_at`` was the dispatch time); after the
routine's max runtime the tick took the queued row for a dead run, closed it
and dispatched another one, and both messages ran when the worker reached
them: ``retry_transient_failures`` queued 50 downloads instead of 25 in an
hour, ``match_likes`` ran twice within minutes.

Here, on the real scheduler (``workers/scheduler.py``) against an isolated
database: the row records when it was queued, when it really started and
when it finished; the message of a run closed while queued does nothing; a
scheduled run that finds the routine already run in its period is skipped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from core.routines import RoutineDef
from db import routine_run_store as runs
from db.models import RoutineRun
from workers import scheduler

from tests.database import new_engine

ROUTINE = RoutineDef(key="busy", task_name="tests.fake.busy", default_interval_minutes=60,
                     min_interval_minutes=1, max_runtime_minutes=60)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def env(monkeypatch):
    engine = new_engine()
    clock = _Clock()
    executions: list[datetime] = []
    queue: list[dict] = []

    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr(scheduler, "_now", clock)
    monkeypatch.setattr(runs, "_now", clock)
    monkeypatch.setattr(scheduler, "ROUTINES", (ROUTINE,))
    monkeypatch.setattr(scheduler, "get_routine", lambda key: ROUTINE if key == ROUTINE.key else None)
    monkeypatch.setattr(scheduler, "_fresh_routine_settings", lambda: {})
    monkeypatch.setattr(scheduler, "celery", SimpleNamespace(tasks={
        ROUTINE.task_name: SimpleNamespace(run=lambda: executions.append(clock.now) or {"done": 1}),
    }))
    monkeypatch.setattr(scheduler.run_routine, "apply_async", lambda kwargs=None, **opts: queue.append(kwargs))
    return SimpleNamespace(engine=engine, clock=clock, executions=executions, queue=queue)


def _latest(env) -> RoutineRun:
    with Session(env.engine) as session:
        return runs.latest_run(session, ROUTINE.key)


def test_a_dispatched_run_is_queued_until_the_worker_starts_it(env):
    scheduler.tick()
    queued = _latest(env)
    assert queued.status == runs.STATUS_QUEUED
    assert queued.queued_at == env.clock.now
    assert queued.started_at is None

    env.clock.advance(minutes=47)  # the queue is busy
    scheduler.run_routine(**env.queue.pop(0))

    done = _latest(env)
    assert done.status == runs.STATUS_OK
    assert done.started_at == env.clock.now  # the real start, not the dispatch
    assert done.queued_at == queued.queued_at


def test_a_backlog_longer_than_the_max_runtime_never_runs_the_routine_twice(env):
    scheduler.tick()
    env.clock.advance(minutes=ROUTINE.max_runtime_minutes + 1)
    # Still in the queue past the max runtime: the next tick may give the
    # message up for lost and queue another one...
    scheduler.tick()
    # ...but when the worker reaches both messages, the routine runs once.
    env.clock.advance(minutes=100)
    for message in list(env.queue):
        scheduler.run_routine(**message)

    assert len(env.executions) == 1


def test_the_next_period_counts_from_the_real_start(env):
    scheduler.tick()
    env.clock.advance(minutes=50)  # waited in the queue
    scheduler.run_routine(**env.queue.pop(0))
    started = env.clock.now

    env.clock.advance(minutes=30)
    scheduler.tick()  # 80 min after the dispatch, 30 after the start: not due
    assert env.queue == []

    env.clock.advance(minutes=30)
    scheduler.tick()
    assert len(env.queue) == 1
    assert env.clock.now - started >= timedelta(minutes=ROUTINE.default_interval_minutes)


def test_a_second_scheduled_run_in_the_same_period_is_skipped(env):
    # Two runs queued for the same period (a tick and a concurrent dispatch
    # from another process both found the routine idle).
    with Session(env.engine) as session:
        first = runs.queue_run(session, ROUTINE.key).id
        second = runs.queue_run(session, ROUTINE.key).id
    period = ROUTINE.default_interval_minutes
    scheduler.run_routine(routine_key=ROUTINE.key, run_id=first, period_minutes=period)
    env.clock.advance(minutes=1)
    scheduler.run_routine(routine_key=ROUTINE.key, run_id=second, period_minutes=period)

    assert len(env.executions) == 1
    with Session(env.engine) as session:
        assert session.get(RoutineRun, second).status == runs.STATUS_SKIPPED


def test_run_now_is_not_held_back_by_the_period(env):
    scheduler.tick()
    scheduler.run_routine(**env.queue.pop(0))
    env.clock.advance(minutes=5)

    assert scheduler.dispatch_routine(ROUTINE) is not None  # "Run now": no period
    scheduler.run_routine(**env.queue.pop(0))

    assert len(env.executions) == 2


def test_a_queued_run_blocks_another_dispatch(env):
    scheduler.tick()
    env.clock.advance(minutes=10)
    assert scheduler.dispatch_routine(ROUTINE) is None
    scheduler.tick()
    assert len(env.queue) == 1
