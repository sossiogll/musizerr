"""Marcatore durevole del reconciler auto-like: idempotenza e sweep."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from db.models import PendingLike
from db.pending_like_store import (
    claim_pending_like,
    ensure_pending_like,
    list_pending_like_ids,
    list_resumable_pending_like_ids,
)

from tests.database import new_engine, seed_parents


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        seed_parents(s, users=("u1", "u2"), tracks=("t1", "t2", "never", "scheduled", "lost"))
        yield s


def test_ensure_is_idempotent_for_same_triple(session):
    a = ensure_pending_like(session, "u1", "t1", "sync")
    b = ensure_pending_like(session, "u1", "t1", "sync")
    assert a == b  # stesso intento → stesso marcatore, nessun duplicato
    assert len(list_pending_like_ids(session)) == 1


def test_one_intent_per_user_and_track(session):
    ensure_pending_like(session, "u1", "t1", "sync")
    ensure_pending_like(session, "u1", "t2", "sync")   # traccia diversa
    ensure_pending_like(session, "u2", "t1", "sync")   # utente diverso
    ensure_pending_like(session, "u1", "t1", "other")  # stesso intento, altro provider: non ne crea un altro
    assert len(list_pending_like_ids(session)) == 3


def test_a_favorite_intent_moves_to_the_sync_phase_never_back(session):
    pid = ensure_pending_like(session, "u1", "t1")
    assert session.get(PendingLike, pid).sync_provider is None
    assert ensure_pending_like(session, "u1", "t1", "sync") == pid
    assert session.get(PendingLike, pid).sync_provider == "sync"
    ensure_pending_like(session, "u1", "t1")
    assert session.get(PendingLike, pid).sync_provider == "sync"


def test_marker_defaults(session):
    pid = ensure_pending_like(session, "u1", "t1", "sync")
    marker = session.get(PendingLike, pid)
    assert marker.attempts == 0
    assert marker.created_at is not None


def test_one_claim_at_a_time(session):
    pid = ensure_pending_like(session, "u1", "t1", "sync")
    now = datetime.now(timezone.utc)
    hold = now + timedelta(minutes=10)

    assert claim_pending_like(session, pid, due_by=now, hold_until=hold) is True
    # Held: a second trigger, now or a minute later, does not get it.
    assert claim_pending_like(session, pid, due_by=now, hold_until=hold) is False
    assert claim_pending_like(session, pid, due_by=now + timedelta(minutes=1), hold_until=hold) is False
    # Due again once the hold (or the scheduled attempt) is reached.
    assert claim_pending_like(session, pid, due_by=hold, hold_until=hold + timedelta(minutes=10)) is True
    assert claim_pending_like(session, "missing", due_by=hold, hold_until=hold) is False


def test_the_sync_phase_starts_with_an_attempt_due_now(session):
    pid = ensure_pending_like(session, "u1", "t1")
    now = datetime.now(timezone.utc)
    claim_pending_like(session, pid, due_by=now, hold_until=now + timedelta(hours=1))

    ensure_pending_like(session, "u1", "t1", "sync")

    session.expire_all()
    assert session.get(PendingLike, pid).next_attempt_at is None


def test_resumable_markers_are_the_ones_without_a_live_chain(session):
    now = datetime.now(timezone.utc)
    never = ensure_pending_like(session, "u1", "never", "sync")
    scheduled = ensure_pending_like(session, "u1", "scheduled", "sync")
    lost = ensure_pending_like(session, "u1", "lost", "sync")
    claim_pending_like(session, scheduled, due_by=now, hold_until=now + timedelta(minutes=30))
    claim_pending_like(session, lost, due_by=now, hold_until=now - timedelta(minutes=30))

    assert set(list_resumable_pending_like_ids(session, lost_before=now - timedelta(minutes=5))) == {never, lost}


def test_list_pending_like_ids_reflects_deletions(session):
    pid = ensure_pending_like(session, "u1", "t1", "sync")
    assert list_pending_like_ids(session) == [pid]
    session.delete(session.get(PendingLike, pid))
    session.commit()
    assert list_pending_like_ids(session) == []
