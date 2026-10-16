"""
Queues and workers in SIMULATED time: routines run on time, once per period,
and a user's job never waits behind the downloads.

Seen live (2 Oct): one worker (concurrency 1) consumed every queue. With
downloads taking up to ~40 s each (FTP timeouts) and the delegation polls
re-queuing themselves every two minutes, the shared queue only grew: routine
runs queued at 15:18 started at 18:05, a user's "Sync likes" waited 11.5
minutes, routines ran twice within minutes.

The harness runs the REAL scheduler (``workers/scheduler.py``: tick, dispatch,
claim, finish) and the REAL library job start/claim (``services/
library_jobs.py``) on an isolated database, with a virtual clock and an
in-memory broker that routes every message with the real route table
(``workers.celery_app.queue_of``). Workers take messages from their queues in
turn (Redis BRPOP rotates the queue order) and stay busy for each task's
duration; beat sends a tick every minute.

- One worker for every queue (the topology before): the user's job and the
  routines wait for the download backlog.
- One worker per queue (docs/workers.md): every routine starts within a tick
  of being due, exactly once per period, and the user's job starts at once,
  while the downloads worker never stops downloading.
"""
from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from core.routines import RoutineDef
from db import library_job_store as job_store
from db import routine_run_store
from db.models import User
from services import library_jobs
from workers import celery_app, scheduler

from tests.database import new_engine

HOURS = 6
DOWNLOAD_SECONDS = 40
DOWNLOADS_QUEUED = 1000
DELEGATION_CHAINS = 20
DELEGATION_POLL_SECONDS = 120
JOB_SECONDS = 45
JOB_REQUESTS_AT = (timedelta(minutes=15), timedelta(hours=2), timedelta(hours=4, minutes=10))
BEAT_SECONDS = 60

TICK = "workers.routines.tick"
RUN = "workers.routines.run"
DOWNLOAD = "workers.tasks.download_task"
POLL = "workers.maintenance.reconcile_delegation"
JOB = library_jobs.TASKS[job_store.KIND_LIBRARY_SYNC]

# name -> (routine, how long one execution takes)
ROUTINES = {
    "every_hour": (RoutineDef(key="every_hour", task_name="sim.every_hour", default_interval_minutes=60), 3),
    "every_half_hour": (RoutineDef(key="every_half_hour", task_name="sim.every_half_hour",
                                   default_interval_minutes=30), 1),
    "slow_hourly": (RoutineDef(key="slow_hourly", task_name="sim.slow_hourly", default_interval_minutes=60), 90),
}


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _Message:
    name: str
    kwargs: dict
    sent_at: datetime
    seq: int


@dataclass
class _Worker:
    queues: tuple[str, ...]
    concurrency: int
    busy_until: list[datetime] = field(default_factory=list)
    turn: int = 0


class _Sim:
    def __init__(self, clock: _Clock, workers: list[_Worker], route=celery_app.queue_of) -> None:
        self.clock = clock
        self.workers = workers
        self.route = route
        self.queues: dict[str, deque[_Message]] = defaultdict(deque)
        self.delayed: list[tuple[datetime, _Message]] = []
        self.seq = itertools.count()
        self.routine_starts: dict[str, list[datetime]] = defaultdict(list)
        self.job_waits: list[timedelta] = []
        self.downloads_done = 0
        consumed = [q for w in workers for q in w.queues]
        assert sorted(set(consumed)) == sorted(consumed), "a queue consumed by two workers"

    # -- broker ---------------------------------------------------------

    def send(self, name: str, kwargs: dict | None = None, countdown: float = 0) -> None:
        message = _Message(name, dict(kwargs or {}), self.clock.now, next(self.seq))
        if countdown:
            self.delayed.append((self.clock.now + timedelta(seconds=countdown), message))
        else:
            self.queues[self.route(name)].append(message)

    def _release_due(self) -> None:
        due = sorted((m for m in self.delayed if m[0] <= self.clock.now), key=lambda m: (m[0], m[1].seq))
        self.delayed = [m for m in self.delayed if m[0] > self.clock.now]
        for _eta, message in due:
            self.queues[self.route(message.name)].append(message)

    def _take(self, worker: _Worker) -> _Message | None:
        for offset in range(len(worker.queues)):
            queue = worker.queues[(worker.turn + offset) % len(worker.queues)]
            if self.queues[queue]:
                worker.turn = (worker.turn + offset + 1) % len(worker.queues)
                return self.queues[queue].popleft()
        return None

    # -- execution ------------------------------------------------------

    def _execute(self, message: _Message) -> float:
        """Runs the task now (its effects at its start) and says how long it
        keeps the worker busy."""
        if message.name == TICK:
            scheduler.tick()
            return 0.2
        if message.name == RUN:
            scheduler.run_routine(**message.kwargs)
            return ROUTINES[message.kwargs["routine_key"]][1]
        if message.name == DOWNLOAD:
            self.downloads_done += 1
            return DOWNLOAD_SECONDS
        if message.name == POLL:
            self.send(POLL, message.kwargs, countdown=DELEGATION_POLL_SECONDS)
            return 1
        if message.name == JOB:
            reporter = library_jobs.JobReporter.claim(
                message.kwargs["user_id"], job_store.KIND_LIBRARY_SYNC, message.kwargs["run_id"],
                session_factory=self.session_factory,
            )
            assert reporter is not None
            with self.session_factory() as session:
                run = session.get(job_store.LibraryJobRun, message.kwargs["run_id"])
                self.job_waits.append(run.started_at - run.created_at)
            reporter.finish(job_store.STATUS_COMPLETED, result={})
            return JOB_SECONDS
        raise AssertionError(f"unexpected task {message.name}")

    def run(self, until: datetime, at_times: dict[datetime, callable]) -> None:
        for worker in self.workers:
            worker.busy_until = [self.clock.now] * worker.concurrency
        next_beat = self.clock.now
        pending = sorted(at_times.items())
        while self.clock.now < until:
            while pending and pending[0][0] <= self.clock.now:
                pending.pop(0)[1]()
            if self.clock.now >= next_beat:
                self.send(TICK)
                next_beat += timedelta(seconds=BEAT_SECONDS)
            self._release_due()
            for worker in self.workers:
                for slot, free_at in enumerate(worker.busy_until):
                    while free_at <= self.clock.now:
                        message = self._take(worker)
                        if message is None:
                            break
                        free_at = self.clock.now + timedelta(seconds=self._execute(message))
                    worker.busy_until[slot] = max(free_at, self.clock.now)
            self.clock.now += timedelta(seconds=1)


@pytest.fixture
def harness(monkeypatch):
    start = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    clock = _Clock(start)
    engine = new_engine()
    def build(workers: list[_Worker], route=celery_app.queue_of) -> _Sim:
        sim = _Sim(clock, workers, route)
        sim.engine = engine
        sim.session_factory = lambda: Session(engine)
        routines = tuple(routine for routine, _ in ROUTINES.values())
        tasks = {
            routine.task_name: SimpleNamespace(run=lambda key=routine.key: sim.routine_starts[key].append(clock.now))
            for routine in routines
        }
        monkeypatch.setattr(scheduler, "engine", engine)
        monkeypatch.setattr(scheduler, "_now", clock)
        monkeypatch.setattr(routine_run_store, "_now", clock)
        monkeypatch.setattr(job_store, "_now", clock)
        monkeypatch.setattr(scheduler, "ROUTINES", routines)
        monkeypatch.setattr(scheduler, "get_routine", lambda key: ROUTINES[key][0] if key in ROUTINES else None)
        monkeypatch.setattr(scheduler, "_fresh_routine_settings", lambda: {})
        monkeypatch.setattr(scheduler, "celery", SimpleNamespace(tasks=tasks))
        monkeypatch.setattr(scheduler.run_routine, "apply_async", lambda kwargs=None, **opts: sim.send(RUN, kwargs))
        monkeypatch.setattr(library_jobs, "celery", SimpleNamespace(
            send_task=lambda name, kwargs=None, task_id=None, **opts: sim.send(name, kwargs)))
        monkeypatch.setattr(library_jobs, "publish_event", lambda event: None)
        return sim

    with Session(engine) as session:
        user = User(username="listener", password_hash="x")
        session.add(user)
        session.commit()
        user_id = user.id

    def load(sim: _Sim) -> dict[datetime, callable]:
        for _ in range(DOWNLOADS_QUEUED):
            sim.send(DOWNLOAD, {})
        for chain in range(DELEGATION_CHAINS):
            sim.send(POLL, {"download_id": f"d{chain}"}, countdown=chain * DELEGATION_POLL_SECONDS / DELEGATION_CHAINS)

        def sync_likes():
            with Session(engine) as session:
                library_jobs.start(session, user_id, job_store.KIND_LIBRARY_SYNC)

        return {start + offset: sync_likes for offset in JOB_REQUESTS_AT}

    return SimpleNamespace(start=start, clock=clock, build=build, load=load)


def _periods_ok(starts: list[datetime], minutes: int) -> bool:
    return all(b - a >= timedelta(minutes=minutes) for a, b in zip(starts, starts[1:]))


def test_one_worker_per_queue_keeps_routines_on_time_and_jobs_unstarved(harness):
    sim = harness.build([
        _Worker(queues=(celery_app.QUEUE_DOWNLOADS,), concurrency=1),
        _Worker(queues=(celery_app.QUEUE_JOBS,), concurrency=2),
        _Worker(queues=(celery_app.QUEUE_MAINTENANCE,), concurrency=2),
    ])
    sim.run(harness.start + timedelta(hours=HOURS), harness.load(sim))

    # The downloads worker downloaded the whole time, one after the other.
    assert sim.downloads_done == HOURS * 3600 // DOWNLOAD_SECONDS
    # The user's jobs started at once, every time.
    assert len(sim.job_waits) == len(JOB_REQUESTS_AT)
    assert max(sim.job_waits) <= timedelta(seconds=2)

    for key, (routine, _duration) in ROUTINES.items():
        starts = sim.routine_starts[key]
        interval = timedelta(minutes=routine.default_interval_minutes)
        # On time: due → dispatched at the next tick → started at once.
        assert starts[0] - harness.start <= timedelta(seconds=BEAT_SECONDS)
        for previous, current in zip(starts, starts[1:]):
            assert interval <= current - previous <= interval + timedelta(seconds=BEAT_SECONDS + 2), key
        # Once per period, no more, no less.
        assert len(starts) == HOURS * 60 // routine.default_interval_minutes, key

    with Session(sim.engine) as session:
        for key in ROUTINES:
            run = routine_run_store.latest_run(session, key)
            # The wait in the queue is a second, never the run's "duration".
            assert run.started_at - run.queued_at <= timedelta(seconds=2)


def _before_the_split(name: str) -> str:
    """The queues before: downloads, and one "default" for everything else."""
    return "downloads" if celery_app.queue_of(name) == celery_app.QUEUE_DOWNLOADS else "default"


def test_one_worker_for_every_queue_starves_jobs_and_routines(harness):
    """The topology before, for contrast: same load, same scheduler."""
    sim = harness.build([_Worker(queues=("downloads", "default"), concurrency=1)], route=_before_the_split)
    sim.run(harness.start + timedelta(hours=HOURS), harness.load(sim))

    assert min(sim.job_waits, default=timedelta.max) > timedelta(minutes=10)
    for key, (routine, _d) in ROUTINES.items():
        assert len(sim.routine_starts[key]) < HOURS * 60 // routine.default_interval_minutes, key
    # Even starved, no routine ever ran twice in one period.
    for key, (routine, _d) in ROUTINES.items():
        assert _periods_ok(sim.routine_starts[key], routine.default_interval_minutes), key
