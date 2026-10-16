"""
A delegated download expires on the time it was really OBSERVED, not on the
wall clock.

Seen live (2 Oct): "Her", "Lost" and "Messy" were dispatched to the external
downloader, then their polls were skipped for hours (the single worker was
busy with downloads) or the downloader did not answer. The first poll that
got an answer after the 12 h max age failed them, not retriable: they could
never be tried again, although nobody had ever said they could not be
downloaded.

Now (``workers.maintenance.reconcile_delegation``):
- only answered polls count, each covering at most two poll intervals since
  the previous answer: time without polls does not use up the max age;
- after the max age of answered polls the delegation fails, not retriable
  (the external system answered all along, without a result);
- after the max wait since the dispatch without that much observation, it
  fails RETRIABLE: it expired without a real answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from core import download_states as ds
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.delegation import DelegationStatus
from db.models import Download, TrackRecord
from services.config_manager import ConfigManager
from tests.database import new_engine
from tests.conftest import FakeDownloadProvider, FakeRedis, FakeRegistry, make_track
from workers import maintenance

POLL = 120


class _Delegate(FakeDownloadProvider):
    """Answers "still nothing" while reachable, raises while not."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self) -> None:
        super().__init__("delegate")
        self.reachable = True

    def poll_delegation(self, handle, session):
        if not self.reachable:
            raise ConnectionError("unreachable")
        return DelegationStatus(state=ds.DISPATCHED, detail="no queue item yet")


@pytest.fixture
def env(monkeypatch):
    engine = new_engine()
    settings_engine = new_engine()
    config = ConfigManager(engine=settings_engine)
    config.update({"delegation_max_age_hours": 12, "delegation_max_wait_hours": 72,
                   "delegation_poll_interval_seconds": POLL})
    clock = {"now": datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)}
    delegate = _Delegate()
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "_now", lambda: clock["now"])
    monkeypatch.setattr(maintenance, "_redis_client", FakeRedis())
    monkeypatch.setattr(maintenance, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    monkeypatch.setattr(maintenance, "get_registry", lambda: FakeRegistry(downloads=[delegate]))
    monkeypatch.setattr(maintenance.reconcile_delegation, "apply_async", lambda kwargs, countdown=None: None)

    track = make_track(title="Her")
    with Session(engine) as session:
        session.add(TrackRecord(internal_id=track.internal_id, title=track.title, artist=track.artist,
                                album=track.album, duration_ms=track.duration_ms))
        session.commit()
        download = Download(track_id=track.internal_id, status=ds.DISPATCHED, delegation_handle="7687",
                            provider_used="delegate", started_at=clock["now"])
        session.add(download)
        session.commit()
        download_id = download.id

    def poll(after: timedelta) -> dict:
        clock["now"] += after
        return maintenance.reconcile_delegation(download_id)

    def record() -> Download:
        with Session(engine) as session:
            return session.get(Download, download_id)

    return type("Env", (), {"poll": staticmethod(poll), "record": staticmethod(record), "delegate": delegate})


def test_polls_skipped_for_longer_than_the_max_age_do_not_expire_it(env):
    result = env.poll(timedelta(hours=13))  # the first poll that ran, 13 h late

    assert result["status"] == ds.DISPATCHED
    record = env.record()
    assert record.status == ds.DISPATCHED
    assert record.delegation_answers == 1
    assert record.delegation_observed_seconds == 2 * POLL  # 13 h of silence count two polls


def test_hours_without_an_answer_do_not_count_either(env):
    env.delegate.reachable = False
    for _ in range(10):
        assert env.poll(timedelta(hours=1))["status"] == "poll-failed"
    env.delegate.reachable = True

    assert env.poll(timedelta(seconds=POLL))["status"] == ds.DISPATCHED
    assert env.record().delegation_observed_seconds == 2 * POLL


def test_twelve_hours_of_answered_polls_expire_it_not_retriable(env):
    polls_in_max_age = 12 * 3600 // POLL
    for _ in range(polls_in_max_age):
        assert env.poll(timedelta(seconds=POLL))["status"] == ds.DISPATCHED

    assert env.poll(timedelta(seconds=POLL)) == {"status": ds.FAILED, "detail": "max-age"}
    record = env.record()
    assert record.retriable is False
    assert record.error_message == maintenance.MAX_AGE_DETAIL
    assert record.delegation_answers == polls_in_max_age + 1


def test_past_the_max_wait_without_enough_answers_it_fails_retriable(env):
    env.delegate.reachable = False
    result = None
    for _ in range(80):
        result = env.poll(timedelta(hours=1))
        if result["status"] == ds.FAILED:
            break

    assert result == {"status": ds.FAILED, "detail": "max-wait"}
    record = env.record()
    assert record.retriable is True
    assert record.error_message == maintenance.MAX_WAIT_DETAIL
    assert record.completed_at is not None


def test_an_answer_after_the_max_wait_also_closes_it_retriable(env):
    for _ in range(3):  # three answers, then the polls stop for days
        env.poll(timedelta(seconds=POLL))

    assert env.poll(timedelta(hours=73)) == {"status": ds.FAILED, "detail": "max-wait"}
    assert env.record().retriable is True
