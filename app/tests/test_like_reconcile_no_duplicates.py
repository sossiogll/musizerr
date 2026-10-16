"""
A like marker gets one queued attempt at a time.

Seen live (2 Oct): after a restart, ``reconcile_pending_likes`` re-queued
every marker whose ``next_attempt_at`` was NULL, even those whose attempt
was already in the queue and not started yet: 236 duplicate tasks. The claim
stopped them, but each still cost a worker turn.

Now the marker is marked queued (``next_attempt_at`` = the enqueue time,
``db.pending_like_store.mark_queued``) BEFORE the task is queued: a second
sweep, or a favorite added again, finds it queued and queues nothing; the
attempt, when it starts, claims it as before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from db.models import Download, PendingLike, TrackRecord, User
from services import favorites
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider
from workers import maintenance


@pytest.fixture
def env(monkeypatch):
    engine = new_engine()
    clock = {"now": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)}
    queued: list[str] = []
    registry = FakeRegistry(syncs=[FakeSyncProvider("nav")])
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "_now", lambda: clock["now"])
    monkeypatch.setattr(favorites, "_now", lambda: clock["now"])
    monkeypatch.setattr(maintenance.reconcile_like, "apply_async",
                        lambda kwargs, countdown=None: queued.append(kwargs["pending_like_id"]))
    monkeypatch.setattr(favorites, "dispatch", lambda name, **kw: queued.append(kw["pending_like_id"]))
    monkeypatch.setattr(favorites, "get_config", lambda: SimpleNamespace(sync_provider="nav"))
    monkeypatch.setattr(maintenance, "get_config", lambda: SimpleNamespace(get=lambda k, d=None: d))
    monkeypatch.setattr(maintenance, "get_registry", lambda: registry)
    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(maintenance, "has_own_account", lambda store, provider: True)
    with Session(engine) as session:
        user = User(username="u", password_hash="x")
        session.add(user)
        session.commit()
        markers = []
        for i in range(5):
            record = TrackRecord(title=f"t{i}", artist="a", album="b")
            session.add(record)
            session.commit()
            session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path=f"a/{i}.flac"))
            # Markers as a restart leaves them: never scheduled.
            marker = PendingLike(user_id=user.id, track_id=record.internal_id, sync_provider="nav")
            session.add(marker)
            session.commit()
            markers.append((marker.id, record.internal_id))
        user_id = user.id
    return SimpleNamespace(engine=engine, clock=clock, queued=queued, markers=markers, user_id=user_id,
                           registry=registry)


def test_a_second_sweep_before_the_attempts_start_queues_nothing(env):
    assert maintenance.reconcile_pending_likes.run() == {"status": "swept", "count": 5}
    env.clock["now"] += timedelta(minutes=1)  # the restart's second run, the queue still busy

    assert maintenance.reconcile_pending_likes.run() == {"status": "swept", "count": 0}
    assert len(env.queued) == 5


def test_the_queued_attempt_still_claims_its_marker(env):
    maintenance.reconcile_pending_likes.run()
    env.clock["now"] += timedelta(seconds=40)  # started 40 s after it was queued

    result = maintenance.reconcile_like.run(pending_like_id=env.markers[0][0])

    assert result["status"] != "not-due"


def test_an_attempt_lost_in_the_queue_is_queued_again_after_the_grace(env):
    maintenance.reconcile_pending_likes.run()
    env.clock["now"] += timedelta(seconds=maintenance._LIKE_LOST_CHAIN_GRACE_SECONDS + 1)

    assert maintenance.reconcile_pending_likes.run()["count"] == 5


def test_a_favorite_added_again_while_its_attempt_is_queued_queues_nothing(env):
    with Session(env.engine) as session:
        marker_id, track_id = env.markers[0]
        favorites._add_in_library(session, env.user_id, track_id, favorites.HEART)
        favorites._add_in_library(session, env.user_id, track_id, favorites.HEART)

    assert env.queued == [marker_id]
