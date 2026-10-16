"""
Transient upstream failures get time to pass.

Real case: the retries waited 10/20/40 s (``retry_delay_seconds * 2**n``), so
an upstream hiccup of a few minutes exhausted every attempt; a bulk run on 7
Sep left tracks failed for weeks, nothing ever looked at them again.

- The retry of a transient failure waits an exponential, capped and jittered
  delay (``core.backoff.retry_delay``) whose base and cap are settings.
- A provider that keeps failing transiently cools down for a while (optional,
  ``provider_cooldown_errors``): downloads skip it and retry later instead of
  burning their attempts on it.
- A routine retries, later, the tracks whose last failure was transient.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from core.backoff import retry_delay
from core.download_resolver import DownloadResolver, ResolveResult
from core.routines import get_routine
from db.models import Download, LibraryEntry, TrackRecord
from services import downloads
from services.config_manager import ConfigManager
from services.provider_cooldown import RedisProviderCooldown
from workers import tasks

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeDownloadProvider, FakeRedis, FakeRegistry, make_track


@pytest.fixture
def engine(monkeypatch):
    eng = new_engine()
    monkeypatch.setattr(tasks, "engine", eng)
    return eng


@pytest.fixture
def config(engine, monkeypatch):
    cm = ConfigManager(engine=engine)
    monkeypatch.setattr(tasks, "get_config", lambda: cm)
    monkeypatch.setattr(downloads, "get_config", lambda: cm)
    return cm


# ------------------------------------------------------------------
# Backoff
# ------------------------------------------------------------------

def test_the_default_backoff_gives_an_upstream_minutes_not_seconds(config):
    waits = [retry_delay(n, config.retry_delay_seconds, 0.0, config.retry_max_delay_seconds)
             for n in range(1, config.max_retries + 1)]

    assert waits[0] >= 60
    assert sum(waits) >= 15 * 60
    assert all(later >= earlier for earlier, later in zip(waits, waits[1:]))
    assert max(waits) <= config.retry_max_delay_seconds


def test_the_jitter_spreads_retries_born_together_without_exceeding_the_cap():
    assert retry_delay(20, 60, 0.0, 3600) == 3600
    assert retry_delay(20, 60, 0.999, 3600) < 3600
    assert retry_delay(1, 60, 0.999, 3600) > 60 * 0.75


def test_a_transient_failure_is_retried_with_the_configured_backoff(engine, config, monkeypatch):
    config.update({"retry_delay_seconds": 120, "retry_max_delay_seconds": 1800, "max_retries": 10})
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (None, "disk"))
    monkeypatch.setattr(tasks.DownloadResolver, "resolve",
                        lambda self, track, workspace, on_trying=None: ResolveResult(success=False, error="503", retriable=True))
    monkeypatch.setattr(tasks.random, "random", lambda: 0.0)
    countdowns: list[float] = []

    class _Retry(Exception):
        pass

    def fake_retry(exc=None, countdown=None, kwargs=None):
        countdowns.append(countdown)
        return _Retry()

    monkeypatch.setattr(tasks.download_task, "retry", fake_retry)
    with Session(engine) as session:
        record = TrackRecord(title="Song", artist="Artist", album="Album", duration_ms=1)
        session.add(record)
        session.commit()
        dl = Download(track_id=record.internal_id, status=ds.PENDING)
        session.add(dl)
        session.commit()
        download_id, track_id = dl.id, record.internal_id
    track = make_track()
    track.internal_id = track_id

    for retries in (0, 3, 9):
        tasks.download_task.push_request(retries=retries)
        try:
            with pytest.raises(_Retry):
                tasks.download_task.run(track_data=track.to_dict(), download_id=download_id)
        finally:
            tasks.download_task.pop_request()

    assert countdowns == [120, 960, 1800]


# ------------------------------------------------------------------
# Per-provider cool-down
# ------------------------------------------------------------------

def test_a_provider_cools_down_after_consecutive_transient_errors():
    clock = [0.0]
    cooldown = RedisProviderCooldown(errors=3, seconds=600, client=FakeRedis(clock=lambda: clock[0]))

    for _ in range(2):
        cooldown.record("dl", ok=False)
    assert cooldown.cooling("dl") is False
    cooldown.record("dl", ok=True)  # an answer resets the streak
    for _ in range(3):
        cooldown.record("dl", ok=False)
    assert cooldown.cooling("dl") is True
    assert cooldown.cooling("other") is False
    clock[0] += 601
    assert cooldown.cooling("dl") is False


def test_the_cool_down_is_off_when_not_configured():
    cooldown = RedisProviderCooldown(errors=0, seconds=600, client=FakeRedis())

    for _ in range(50):
        cooldown.record("dl", ok=False)

    assert cooldown.cooling("dl") is False


class _Flaky(FakeDownloadProvider):
    supported_identifier_types = {"isrc"}

    def __init__(self):
        super().__init__("flaky")
        self.calls = 0

    def download_by_id(self, identifier, workspace, session):
        self.calls += 1
        raise ConnectionError("upstream 503")


def test_the_resolver_skips_a_cooling_provider_and_the_download_is_retried_later(tmp_path):
    flaky = _Flaky()
    cooldown = RedisProviderCooldown(errors=2, seconds=600, client=FakeRedis())
    resolver = DownloadResolver(FakeRegistry(downloads=[flaky]), FakeAuthManager(), ["flaky"], cooldown=cooldown)
    track = make_track(isrc="X1")

    results = [resolver.resolve(track, lambda name: str(tmp_path)) for _ in range(4)]

    assert flaky.calls == 2
    assert all(not r.success and r.retriable for r in results)


# ------------------------------------------------------------------
# The routine that retries transient failures later
# ------------------------------------------------------------------

def _track(session, title: str) -> str:
    record = TrackRecord(title=title, artist="Artist", album="Album", duration_ms=1)
    session.add(record)
    session.commit()
    return record.internal_id


def _failed(session, track_id: str, *, age_hours: float, retriable: bool | None) -> None:
    at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    session.add(Download(track_id=track_id, status=ds.FAILED, retriable=retriable, started_at=at, completed_at=at))
    session.commit()


def test_the_routine_retries_old_transient_failures_only(engine, config, monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    config.update({"transient_retry_after_hours": 6, "transient_retry_max_rounds": 2, "transient_retry_batch_size": 10})
    with Session(engine) as session:
        due = _track(session, "Due")
        _failed(session, due, age_hours=7, retriable=True)
        legacy = _track(session, "Legacy")  # failed before the flag existed: unknown kind
        _failed(session, legacy, age_hours=24 * 20, retriable=None)
        recent = _track(session, "Recent")
        _failed(session, recent, age_hours=1, retriable=True)
        definitive = _track(session, "Definitive")
        _failed(session, definitive, age_hours=7, retriable=False)
        exhausted = _track(session, "Exhausted")
        _failed(session, exhausted, age_hours=30, retriable=True)
        _failed(session, exhausted, age_hours=7, retriable=True)
        owned = _track(session, "Owned")
        _failed(session, owned, age_hours=7, retriable=True)
        session.add(LibraryEntry(track_id=owned, file_path="o.ogg", file_format="ogg", source="scan", storage_provider="disk"))
        session.commit()

    result = tasks.retry_transient_failures()

    with Session(engine) as session:
        pending = {d.track_id for d in session.exec(select(Download).where(Download.status == ds.PENDING)).all()}
    assert pending == {due, legacy}
    assert result["queued"] == 2
    assert len(queued) == 2


def test_the_routine_is_declared():
    routine = get_routine("retry_transient_failures")

    assert routine is not None
    assert routine.task_name == "workers.tasks.retry_transient_failures"


def test_the_cool_down_is_on_by_default(config):
    assert config.provider_cooldown_errors > 0
    assert config.provider_cooldown_seconds > 0


def test_a_skipped_cooling_provider_tells_how_long_to_wait(tmp_path):
    clock = [0.0]
    cooldown = RedisProviderCooldown(errors=1, seconds=600, client=FakeRedis(clock=lambda: clock[0]))
    resolver = DownloadResolver(FakeRegistry(downloads=[_Flaky()]), FakeAuthManager(), ["flaky"], cooldown=cooldown)
    track = make_track(isrc="X1")

    failed = resolver.resolve(track, lambda name: str(tmp_path))  # the error that starts the pause
    clock[0] += 100
    skipped = resolver.resolve(track, lambda name: str(tmp_path))

    assert failed.retry_after == 0
    assert skipped.retriable and skipped.retry_after == 500


def test_a_retry_does_not_come_back_before_the_pause_ends(engine, config, monkeypatch):
    config.update({"retry_delay_seconds": 60, "retry_max_delay_seconds": 3600, "max_retries": 5})
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (None, "disk"))
    monkeypatch.setattr(tasks.DownloadResolver, "resolve", lambda self, track, workspace, on_trying=None: ResolveResult(
        success=False, error="cooling", retriable=True, retry_after=840))
    countdowns: list[float] = []

    class _Retry(Exception):
        pass

    monkeypatch.setattr(tasks.download_task, "retry", lambda exc=None, countdown=None, kwargs=None: (countdowns.append(countdown), _Retry())[1])
    with Session(engine) as session:
        record = TrackRecord(title="Song", artist="Artist", album="Album", duration_ms=1)
        session.add(record)
        session.commit()
        dl = Download(track_id=record.internal_id, status=ds.PENDING)
        session.add(dl)
        session.commit()
        download_id, track_id = dl.id, record.internal_id
    track = make_track()
    track.internal_id = track_id

    with pytest.raises(_Retry):
        tasks.download_task.run(track_data=track.to_dict(), download_id=download_id)

    assert countdowns == [840]
