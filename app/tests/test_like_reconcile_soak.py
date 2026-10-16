"""
Soak test of the like reconciliation in SIMULATED time.

An in-memory broker runs every Celery task at its ETA (``countdown``) on a
virtual clock; the beat runs the hourly sweep (``reconcile_pending_likes``);
the real ``services.favorites`` path re-triggers every marker every ten
minutes (what a favorite added again, a like matching run or a landing does);
at the start each marker also has the duplicate chains a previous version
left in the queue. The sync provider never indexes some tracks and indexes
the others late.

Reproduced in production before the fix: 6–7 parallel chains per marker, a
new one at every trigger, each re-queuing every 60 s — 830+ runs/min for 139
markers. Here: at most one live chain per marker at any time, runs per hour
bounded by the backoff schedule, and every never-indexed marker given up at
the max age.
"""
from __future__ import annotations

import heapq
import itertools
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from core import like_reconciliation as rules
from core.backoff import retry_delay
from musizerr_sdk.interfaces import SyncCandidate
from db.models import Download, PendingLike, TrackRecord, User
from services import favorites
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider
from workers import maintenance

SYNC = "soaksync"
BASE_INTERVAL_S = 60
MAX_AGE_H = 6
SIM_HOURS = 8
BEAT_S = 3600
TRIGGER_STORM_S = 600
LEGACY_CHAINS = 6
RECONCILE = "workers.maintenance.reconcile_like"
SWEEP = "workers.maintenance.reconcile_pending_likes"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@dataclass(order=True)
class _Task:
    eta: datetime
    seq: int
    name: str = field(compare=False)
    kwargs: dict = field(compare=False)
    origin: str = field(compare=False)


class _Broker:
    """Runs each task at its ETA on the virtual clock, in ETA order."""

    def __init__(self, clock: _Clock, max_runs: int) -> None:
        self.clock = clock
        self.queue: list[_Task] = []
        self.seq = itertools.count()
        self.running: _Task | None = None
        self.max_runs = max_runs
        self.results: list[tuple[datetime, str, dict, dict]] = []

    def send(self, name: str, kwargs: dict, countdown: float = 0, origin: str | None = None) -> None:
        if origin is None:
            # A reconcile_like that re-queues itself continues its chain.
            origin = "chain" if self.running and self.running.name == RECONCILE and name == RECONCILE else "trigger"
        eta = self.clock.now + timedelta(seconds=countdown or 0)
        heapq.heappush(self.queue, _Task(eta, next(self.seq), name, dict(kwargs), origin))

    def live_chains(self) -> Counter:
        return Counter(t.kwargs["pending_like_id"] for t in self.queue if t.name == RECONCILE and t.origin == "chain")

    def run_until(self, end: datetime, handlers: dict, after_each) -> None:
        while self.queue and self.queue[0].eta <= end:
            task = heapq.heappop(self.queue)
            self.clock.now = max(self.clock.now, task.eta)
            self.running = task
            result = handlers[task.name](**task.kwargs)
            self.running = None
            if task.name == RECONCILE:
                self.results.append((self.clock.now, task.origin, task.kwargs, result))
                assert len(self.results) <= self.max_runs, f"runaway reconciliation: {len(self.results)} runs"
            after_each()
        self.clock.now = end


class _LateSync(FakeSyncProvider):
    """Indexes each known title at its time; never the others. Counts the
    searches (one search per resolution attempt)."""

    def __init__(self, clock: _Clock, indexed_at: dict[str, datetime]) -> None:
        super().__init__(SYNC)
        self.clock = clock
        self.indexed_at = indexed_at
        self.searches: list[datetime] = []

    def find_candidates(self, track, session):
        self.searches.append(self.clock.now)
        at = self.indexed_at.get(track.title)
        if at is not None and self.clock.now >= at:
            return [SyncCandidate(sync_id=f"sid-{track.title}", track=track)]
        return []


@pytest.fixture
def soak(monkeypatch):
    start = datetime.now(timezone.utc)
    clock = _Clock(start)
    engine = new_engine()
    titles = [f"never-{i}" for i in range(20)] + [f"soon-{i}" for i in range(10)] + [f"late-{i}" for i in range(10)]
    indexed_at = {t: start + timedelta(minutes=30) for t in titles if t.startswith("soon")}
    indexed_at.update({t: start + timedelta(hours=3) for t in titles if t.startswith("late")})
    sync = _LateSync(clock, indexed_at)
    registry = FakeRegistry(syncs=[sync])
    settings = {"like_reconcile_interval_seconds": BASE_INTERVAL_S, "like_reconcile_max_age_hours": MAX_AGE_H}
    broker = _Broker(clock, max_runs=20_000)

    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "_now", clock)
    monkeypatch.setattr(maintenance, "random", random.Random(20260929))
    monkeypatch.setattr(maintenance, "get_config", lambda: SimpleNamespace(get=lambda k, d=None: settings.get(k, d)))
    monkeypatch.setattr(maintenance, "get_registry", lambda: registry)
    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(maintenance, "has_own_account", lambda store, provider: True)
    monkeypatch.setattr(
        maintenance.reconcile_like, "apply_async",
        lambda kwargs, countdown=None: broker.send(RECONCILE, kwargs, countdown or 0),
    )
    monkeypatch.setattr(favorites, "get_config", lambda: SimpleNamespace(sync_provider=SYNC))
    monkeypatch.setattr(favorites, "_now", clock)
    monkeypatch.setattr(favorites, "dispatch", lambda task_name, **kwargs: broker.send(task_name, kwargs))

    with Session(engine) as session:
        user = User(username="listener", password_hash="x", role="user")
        session.add(user)
        session.commit()
        user_id = user.id
        track_ids = {}
        for title in titles:
            record = TrackRecord(title=title, artist="Artist", album="Album")
            session.add(record)
            session.commit()
            session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path=f"A/{title}.flac",
                                 completed_at=start))
            session.commit()
            track_ids[title] = record.internal_id
        # The heart on each track: favorite + like to propagate (dispatches a run).
        for track_id in track_ids.values():
            favorites.add(session, user_id, track_id, registry)
        markers = {m.track_id: m.id for m in session.exec(select(PendingLike)).all()}

    # The duplicate chains an earlier version left queued for every marker.
    for marker_id in markers.values():
        for _ in range(LEGACY_CHAINS):
            broker.send(RECONCILE, {"pending_like_id": marker_id}, 0, origin="legacy")

    def storm():
        with Session(engine) as session:
            for marker in session.exec(select(PendingLike)).all():
                favorites._add_in_library(session, marker.user_id, marker.track_id, favorites.HEART)
        broker.send("storm", {}, TRIGGER_STORM_S, origin="clock")

    def beat():
        maintenance.reconcile_pending_likes.run()
        broker.send("beat", {}, BEAT_S, origin="clock")

    broker.send("storm", {}, TRIGGER_STORM_S, origin="clock")
    broker.send("beat", {}, BEAT_S, origin="clock")
    handlers = {
        RECONCILE: lambda **kw: maintenance.reconcile_like.run(**kw),
        "storm": storm,
        "beat": beat,
    }
    return SimpleNamespace(
        start=start, clock=clock, broker=broker, handlers=handlers, sync=sync, engine=engine,
        titles=titles, track_ids=track_ids, markers=markers,
    )


def _run(soak, until: datetime, max_live: Counter) -> None:
    def after_each():
        for marker_id, count in soak.broker.live_chains().items():
            max_live[marker_id] = max(max_live[marker_id], count)

    soak.broker.run_until(until, soak.handlers, after_each)


def _schedule_bounds(max_age_s: float) -> tuple[int, int]:
    """Claimed runs of a never-indexed marker up to its give-up: the first
    run, the retries whose cumulative wait stays within the max age, and the
    run past it that gives up. Bounds from the longest and shortest jitter."""
    def runs(jitter: float) -> int:
        elapsed, attempt = 0.0, 0
        while elapsed <= max_age_s:
            attempt += 1
            elapsed += retry_delay(attempt, BASE_INTERVAL_S, jitter, rules.MAX_RETRY_INTERVAL_SECONDS)
        return attempt + 1

    return runs(0.0), runs(1.0)


def test_one_chain_per_marker_bounded_runs_and_give_up_at_max_age(soak):
    max_live: Counter = Counter()
    lost_at = soak.start + timedelta(hours=2, minutes=5)

    _run(soak, lost_at, max_live)
    # A worker restart loses the scheduled continuation of five markers.
    lost = [soak.markers[soak.track_ids[f"never-{i}"]] for i in range(5)]
    soak.broker.queue = [t for t in soak.broker.queue if not (t.origin == "chain" and t.kwargs["pending_like_id"] in lost)]
    heapq.heapify(soak.broker.queue)
    with Session(soak.engine) as session:
        was_due = {m: session.get(PendingLike, m).next_attempt_at.replace(tzinfo=timezone.utc) for m in lost}
    _run(soak, soak.start + timedelta(hours=SIM_HOURS), max_live)

    results = soak.broker.results
    claimed = [(at, kw["pending_like_id"], r) for at, _origin, kw, r in results if r["status"] not in ("not-due", "gone")]
    by_marker: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for at, marker_id, result in claimed:
        by_marker[marker_id].append((at, result))

    # 1. Never more than one live chain per marker, legacy duplicates and
    #    trigger storms included.
    assert max(max_live.values()) == 1

    # 2. Runs per hour follow the backoff schedule, not the trigger count.
    max_age_s = MAX_AGE_H * 3600
    low, high = _schedule_bounds(max_age_s)
    never = {soak.markers[soak.track_ids[t]] for t in soak.titles if t.startswith("never")}
    for marker_id in never - set(lost):
        assert low <= len(by_marker[marker_id]) <= high, (marker_id, len(by_marker[marker_id]))
    for marker_id in lost:  # resumed late by the sweep: one run fewer at most
        assert low - 1 <= len(by_marker[marker_id]) <= high
    per_hour = Counter(int((at - soak.start).total_seconds() // 3600) for at, _m, _r in claimed)
    markers_total = len(soak.markers)
    for hour in range(2, MAX_AGE_H):
        # Past the first hour every waiting marker is on the hourly step.
        assert per_hour[hour] <= 2 * markers_total, (hour, per_hour[hour])
    # The extra triggers never searched: the legacy chains stopped at the
    # claim, and a storm or a sweep finding a marker queued or scheduled
    # queued nothing at all (``mark_queued``) — the only wasted runs are the
    # legacy duplicates.
    assert len(soak.sync.searches) == sum(
        1 for _at, _m, r in claimed if r["status"] in ("re-queued", "liked", "gave-up")
    )
    assert len(results) - len(claimed) == LEGACY_CHAINS * markers_total

    # 3. A lost chain comes back with the first trigger that finds it due
    #    (the storm re-triggers every marker) or the first sweep that finds it
    #    overdue past the grace, whichever comes first.
    grace = timedelta(seconds=maintenance._LIKE_LOST_CHAIN_GRACE_SECONDS)
    tolerance = timedelta(seconds=maintenance._LIKE_DUE_TOLERANCE_SECONDS)
    for marker_id in lost:
        beats = (soak.start + timedelta(seconds=BEAT_S * k) for k in itertools.count(1))
        storms = (soak.start + timedelta(seconds=TRIGGER_STORM_S * k) for k in itertools.count(1))
        expected = min(
            next(b for b in beats if b > was_due[marker_id] + grace),
            next(s for s in storms if s + tolerance >= was_due[marker_id]),
        )
        after_loss = [at for at, _r in by_marker[marker_id] if at > lost_at]
        assert after_loss[0] == expected

    # 4. Give up at the max age — not before, not hours after.
    for marker_id in never:
        at, result = by_marker[marker_id][-1]
        assert result["status"] == "gave-up"
        age = (at - soak.start).total_seconds()
        assert max_age_s < age <= max_age_s + rules.MAX_RETRY_INTERVAL_SECONDS + 60

    # 5. Tracks indexed late are liked soon after, then left alone.
    for title in soak.titles:
        if title.startswith("never"):
            continue
        marker_id = soak.markers[soak.track_ids[title]]
        at, result = by_marker[marker_id][-1]
        assert result["status"] == "liked"
        indexed = soak.sync.indexed_at[title]
        assert indexed <= at <= indexed + timedelta(hours=1)
    assert sorted(soak.sync.liked) == sorted(f"sid-{t}" for t in soak.titles if not t.startswith("never"))

    with Session(soak.engine) as session:
        assert session.exec(select(PendingLike)).all() == []
    assert soak.broker.live_chains() == Counter()


def test_the_sweep_leaves_a_live_chain_alone_and_resumes_a_lost_one(soak):
    max_live: Counter = Counter()
    _run(soak, soak.start + timedelta(minutes=5), max_live)
    assert maintenance.reconcile_pending_likes.run() == {"status": "swept", "count": 0}

    # One chain lost: its attempt was due a while ago and never ran.
    marker_id = next(iter(soak.markers.values()))
    soak.broker.queue = [t for t in soak.broker.queue if t.kwargs.get("pending_like_id") != marker_id]
    heapq.heapify(soak.broker.queue)
    grace = maintenance._LIKE_LOST_CHAIN_GRACE_SECONDS

    def overdue_by(seconds: int) -> None:
        with Session(soak.engine) as session:
            marker = session.get(PendingLike, marker_id)
            marker.next_attempt_at = soak.clock.now - timedelta(seconds=seconds)
            session.add(marker)
            session.commit()

    # Late, but within the grace: the chain may still be on its way.
    overdue_by(grace - 1)
    assert maintenance.reconcile_pending_likes.run()["count"] == 0
    overdue_by(grace + 1)
    assert maintenance.reconcile_pending_likes.run()["count"] == 1
    assert [t.kwargs for t in soak.broker.queue if t.name == RECONCILE and t.origin == "trigger"] == [
        {"pending_like_id": marker_id}
    ]
