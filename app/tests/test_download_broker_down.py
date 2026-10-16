"""
The task broker is down while a download is requested. The gate has already
committed the ``Download``; if the message cannot be sent no worker will ever
run it, so it must not stay an open request that later requests join. It ends
as a retriable transient failure (``queue_unavailable``), which the
transient-failure routine queues again once the broker is back. Same for every
caller: a user's request, an admin's, an approval.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlmodel import Session

from core import download_states as ds
from db.models import Download
from services import downloads

from tests.download_requests_support import Harness, downloads_of, intents_of, requests_of


@pytest.fixture
def h(engine, monkeypatch):
    harness = Harness(engine, monkeypatch)
    harness.policy(all_modes="auto")
    return harness


def _broker(h: Harness, monkeypatch, *, up: bool) -> None:
    def down(**kwargs):
        raise ConnectionError("[Errno 111] Connection refused (amqp)")

    monkeypatch.setattr(downloads, "queue_download", (lambda **kw: h.queued.append(kw)) if up else down)


def _assert_dead_rows_are_transient_failures(h: Harness, count: int) -> None:
    rows = downloads_of(h.engine)
    assert len(rows) == count
    for row in rows:
        assert (row.status, row.retriable) == (ds.FAILED, True)
        assert row.error_message.startswith(f"{ds.QUEUE_UNAVAILABLE}: ")
        assert row.completed_at is not None   # terminal states, and only they, are completed
    assert not [r for r in rows if r.status in ds.OPEN]


def _age(h: Harness, days: int = 2) -> None:
    """The failures happened a while ago (the routine waits before retrying)."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=days)
    with Session(h.engine) as session:
        session.exec(update(Download).values(started_at=long_ago, completed_at=long_ago))
        session.commit()


def _routine(h: Harness) -> dict:
    with Session(h.engine) as session:
        return downloads.retry_transient_failures(session, h.registry, h.config)


@pytest.mark.parametrize("who", ["user", "admin"])
def test_a_download_that_cannot_be_queued_is_a_retriable_failure_not_an_orphan(h, monkeypatch, who):
    _broker(h, monkeypatch, up=False)

    resp = h.call(who, "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})

    assert resp.status_code == 200 and resp.json()["status"] == downloads.QUEUED and resp.json()["download_id"]
    _assert_dead_rows_are_transient_failures(h, 1)
    assert h.queued == []
    assert resp.json()["track"]["download_status"] == ds.FAILED

    # The file state now says failed, with the stable code, and a later request does not join a dead row.
    listed = h.call(who, "GET", "/downloads").json()["items"][0]
    assert listed["status"] == ds.FAILED
    assert listed["attempts"][0]["error_code"] == ds.QUEUE_UNAVAILABLE

    _broker(h, monkeypatch, up=True)
    again = h.call(who, "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})
    assert again.json()["status"] == downloads.QUEUED and again.json()["download_id"] != resp.json()["download_id"]
    assert len(h.queued) == 1 and len(downloads_of(h.engine)) == 2


def test_the_routine_queues_the_dead_download_again_when_the_broker_is_back(h, monkeypatch):
    _broker(h, monkeypatch, up=False)
    h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})
    _age(h)

    _broker(h, monkeypatch, up=True)
    result = _routine(h)

    assert result == {"candidates": 1, "queued": 1}
    assert len(h.queued) == 1 and h.queued[0]["user_id"] == h.id("user")
    rows = sorted(downloads_of(h.engine), key=lambda r: r.status)
    assert [r.status for r in rows] == [ds.FAILED, ds.PENDING]


def test_the_routine_with_the_broker_still_down_leaves_a_failure_not_an_orphan(h, monkeypatch):
    _broker(h, monkeypatch, up=False)
    h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})
    _age(h)

    result = _routine(h)

    assert result == {"candidates": 1, "queued": 1}      # the gate accepted it; it failed to leave
    _assert_dead_rows_are_transient_failures(h, 2)
    assert h.queued == []


def test_an_approval_with_the_broker_down_is_approved_and_its_downloads_retry_by_themselves(h, monkeypatch):
    h.policy(playlist_mode="approval")
    request_id = h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-p-3"}).json()["request_id"]
    _broker(h, monkeypatch, up=False)

    approved = h.decide("admin", "approve", request_id)

    assert approved["outcome"] == "decided"
    request = requests_of(h.engine)[0]
    assert (request.status, request.needed_tracks, request.queued_tracks) == ("approved", 3, 3)
    _assert_dead_rows_are_transient_failures(h, 3)
    assert len(intents_of(h.engine)) == 3          # the favorites on arrival are kept for the retry
    assert h.queued == []

    # A later request for the same tracks does not join a dead row, and a second approval does nothing.
    again = h.decide("admin2", "approve", request_id)
    assert again["outcome"] == "unchanged" and len(downloads_of(h.engine)) == 3

    _age(h)
    _broker(h, monkeypatch, up=True)
    assert _routine(h) == {"candidates": 3, "queued": 3}
    assert len(h.queued) == 3
    assert {q["user_id"] for q in h.queued} == {h.id("user")}


def test_a_request_that_joins_a_download_whose_message_was_lost_is_not_left_waiting_forever(h, monkeypatch):
    """Two users ask for the same track with the broker down: the second does not attach to the first's
    dead row, it gets a download of its own."""
    _broker(h, monkeypatch, up=False)
    h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})

    second = h.call("other", "POST", "/download/track", json={"provider": "catalog", "id": "track-one"})

    assert second.json()["status"] == downloads.QUEUED
    _assert_dead_rows_are_transient_failures(h, 2)
