"""``db/routine_run_store.py`` — cronologia di esecuzione dei "system
routines" (tabella ``routine_runs``): PURA data-access, nessuna conoscenza
dei routine dichiarati (usa chiavi opache). Una run: queued → running →
ok / error / skipped, transizioni condizionali sullo stato atteso.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from db import routine_run_store as store
from db.models import RoutineRun



@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _started(session, key: str, *, ago: timedelta = timedelta(0)) -> RoutineRun:
    run = store.queue_run(session, key)
    assert store.claim_run(session, run.id) is not None
    run.started_at = run.queued_at = datetime.now(timezone.utc) - ago
    session.add(run)
    session.commit()
    return run


def test_queue_run_creates_a_queued_row_not_yet_started(session):
    run = store.queue_run(session, "r1")
    assert run.status == store.STATUS_QUEUED
    assert run.queued_at is not None
    assert run.started_at is None and run.finished_at is None
    assert store.latest_run(session, "r1").id == run.id


def test_claim_run_starts_a_queued_run_once(session):
    run = store.queue_run(session, "r1")

    claimed = store.claim_run(session, run.id)
    assert claimed is not None
    assert claimed.status == store.STATUS_RUNNING
    assert claimed.started_at is not None
    assert store.claim_run(session, run.id) is None  # a second message for it


def test_claim_run_with_a_period_skips_when_another_run_started_in_it(session):
    _started(session, "r1", ago=timedelta(minutes=10))
    run = store.queue_run(session, "r1")

    assert store.claim_run(session, run.id, period_seconds=3600) is None
    fetched = session.get(RoutineRun, run.id)
    assert fetched.status == store.STATUS_SKIPPED
    assert fetched.started_at is None
    assert fetched.detail == store.SKIPPED_DETAIL


def test_claim_run_with_a_period_starts_once_the_period_is_over(session):
    _started(session, "r1", ago=timedelta(minutes=61))
    run = store.queue_run(session, "r1")

    assert store.claim_run(session, run.id, period_seconds=3600) is not None


def test_finish_run_sets_status_detail_and_finished_at(session):
    run = _started(session, "r1")
    assert store.finish_run(session, run.id, status=store.STATUS_OK, detail="done: 3 items") is True

    fetched = session.get(RoutineRun, run.id)
    assert fetched.status == store.STATUS_OK
    assert fetched.detail == "done: 3 items"
    assert fetched.finished_at is not None


def test_finish_run_does_not_revive_a_run_closed_as_stale(session):
    run = _started(session, "r1", ago=timedelta(hours=2))
    store.reap_stale_run(session, "r1", stale_after_seconds=3600)

    assert store.finish_run(session, run.id, status=store.STATUS_OK) is False
    assert session.get(RoutineRun, run.id).status == store.STATUS_ERROR


def test_finish_run_on_unknown_id_is_a_noop(session):
    # Nessuna eccezione: la run potrebbe essere sparita (potata).
    assert store.finish_run(session, "does-not-exist", status=store.STATUS_OK) is False


def test_active_run_is_a_queued_or_running_row(session):
    queued = store.queue_run(session, "r1")
    assert store.active_run(session, "r1").id == queued.id
    store.claim_run(session, queued.id)
    assert store.active_run(session, "r1").status == store.STATUS_RUNNING


def test_active_run_none_once_finished_or_never_run(session):
    run = _started(session, "r1")
    store.finish_run(session, run.id, status=store.STATUS_OK)
    assert store.active_run(session, "r1") is None
    assert store.active_run(session, "never-run") is None


def test_active_run_none_for_a_stale_row(session):
    _started(session, "r1", ago=timedelta(hours=2))
    assert store.active_run(session, "r1", stale_after_seconds=3600) is None


def test_reap_stale_run_closes_an_orphaned_running_row_as_error(session):
    run = _started(session, "r1", ago=timedelta(hours=2))

    store.reap_stale_run(session, "r1", stale_after_seconds=3600)

    fetched = session.get(RoutineRun, run.id)
    assert fetched.status == store.STATUS_ERROR
    assert fetched.detail == store.STALE_DETAIL
    assert fetched.finished_at is not None


def test_reap_stale_run_closes_a_run_queued_too_long_and_its_message_runs_nothing(session):
    run = store.queue_run(session, "r1")
    run.queued_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(run)
    session.commit()

    store.reap_stale_run(session, "r1", stale_after_seconds=3600)

    assert session.get(RoutineRun, run.id).detail == store.LOST_DETAIL
    assert store.claim_run(session, run.id) is None


def test_a_running_row_is_stale_from_its_start_not_its_dispatch(session):
    run = store.queue_run(session, "r1")
    run.queued_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(run)
    session.commit()
    store.claim_run(session, run.id)  # started now, after two hours in the queue

    store.reap_stale_run(session, "r1", stale_after_seconds=3600)

    assert session.get(RoutineRun, run.id).status == store.STATUS_RUNNING


def test_reap_stale_run_leaves_a_finished_row_alone(session):
    run = _started(session, "r1")
    store.finish_run(session, run.id, status=store.STATUS_ERROR, detail="real failure")

    store.reap_stale_run(session, "r1", stale_after_seconds=0)

    fetched = session.get(RoutineRun, run.id)
    assert fetched.detail == "real failure"


def test_last_started_at_ignores_runs_that_never_started(session):
    started = _started(session, "r1", ago=timedelta(minutes=30))
    store.queue_run(session, "r1")

    assert store.last_started_at(session, "r1") == started.started_at.replace(tzinfo=timezone.utc)
    assert store.last_started_at(session, "never-run") is None


def test_prune_runs_keeps_only_the_most_recent_n(session):
    ids = []
    for i in range(5):
        run = store.queue_run(session, "r1")
        run.queued_at = datetime.now(timezone.utc) - timedelta(minutes=5 - i)
        session.add(run)
        session.commit()
        ids.append(run.id)

    store.prune_runs(session, "r1", keep=2)

    remaining = session.exec(
        select(RoutineRun).where(RoutineRun.routine_key == "r1")
    ).all()
    assert {r.id for r in remaining} == set(ids[-2:])


def test_prune_runs_is_a_noop_when_under_the_limit(session):
    store.queue_run(session, "r1")
    store.prune_runs(session, "r1", keep=50)

    remaining = session.exec(
        select(RoutineRun).where(RoutineRun.routine_key == "r1")
    ).all()
    assert len(remaining) == 1


def test_prune_runs_does_not_touch_other_routines(session):
    store.queue_run(session, "r1")
    store.queue_run(session, "r2")

    store.prune_runs(session, "r1", keep=0)

    assert store.latest_run(session, "r1") is None
    assert store.latest_run(session, "r2") is not None
