"""
``services/stream.py::resolve_sync_id``: cache su DB, poi lookup via
``SyncProvider.find_candidates`` (tramite ``services.enrichment.
check_sync_availability``) solo quando la cache non ha nulla.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.identifiers import sync_id_type
from db.models import TrackIdentifierRecord, TrackRecord
from services.stream import resolve_sync_id

from tests.conftest import FakeAuthManager, FakeSyncProvider
from tests.loopback import as_proxy


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _make_track_record(session, *, title="Song A", artist="Artist", album="Album") -> str:
    record = TrackRecord(title=title, artist=artist, album=album)
    session.add(record)
    session.commit()
    session.refresh(record)
    return record.internal_id


def _identifier_rows(session, track_id: str, id_type: str) -> list[TrackIdentifierRecord]:
    return list(session.exec(
        select(TrackIdentifierRecord).where(
            TrackIdentifierRecord.track_id == track_id,
            TrackIdentifierRecord.id_type == id_type,
        )
    ).all())


def test_returns_the_cached_sync_id_without_touching_the_provider(session):
    track_id = _make_track_record(session)
    sync = FakeSyncProvider(available={"song a": "should-not-be-used"})
    auth_manager = FakeAuthManager()
    session.add(TrackIdentifierRecord(
        track_id=track_id, id_type=sync_id_type(sync.name), value="cached-sync-id",
    ))
    session.commit()

    result = resolve_sync_id(session, track_id, as_proxy(sync), auth_manager, store=None)

    assert result == "cached-sync-id"
    assert sync.availability_calls == 0
    assert auth_manager.auth_calls == 0


def test_no_cache_and_available_returns_and_persists_the_sync_id(session):
    track_id = _make_track_record(session, title="Song A", artist="Artist", album="Album")
    sync = FakeSyncProvider(available={"song a": "fresh-sync-id"})
    auth_manager = FakeAuthManager()

    result = resolve_sync_id(session, track_id, as_proxy(sync), auth_manager, store=None)

    assert result == "fresh-sync-id"
    assert auth_manager.auth_calls == 1
    rows = _identifier_rows(session, track_id, sync_id_type(sync.name))
    assert len(rows) == 1
    assert rows[0].value == "fresh-sync-id"


def test_no_cache_and_not_available_returns_none_and_persists_nothing(session):
    track_id = _make_track_record(session, title="Song A")
    sync = FakeSyncProvider(available={})  # nessuna traccia disponibile
    auth_manager = FakeAuthManager()

    result = resolve_sync_id(session, track_id, as_proxy(sync), auth_manager, store=None)

    assert result is None
    assert _identifier_rows(session, track_id, sync_id_type(sync.name)) == []


def test_unknown_track_id_returns_none_without_authenticating(session):
    sync = FakeSyncProvider(available={"song a": "sync-id"})
    auth_manager = FakeAuthManager()

    result = resolve_sync_id(session, "does-not-exist", as_proxy(sync), auth_manager, store=None)

    assert result is None
    assert auth_manager.auth_calls == 0


class _RaisingAuthManager:
    def authenticate(self, provider):
        raise RuntimeError("auth boom")


class _RaisingSyncProvider(FakeSyncProvider):
    def find_candidates(self, track, session):
        raise RuntimeError("find_candidates boom")


def test_authenticate_raising_returns_none_instead_of_propagating(session):
    track_id = _make_track_record(session)
    sync = FakeSyncProvider(available={"song a": "sync-id"})

    result = resolve_sync_id(session, track_id, as_proxy(sync), _RaisingAuthManager(), store=None)

    assert result is None
    assert _identifier_rows(session, track_id, sync_id_type(sync.name)) == []


def test_availability_check_raising_returns_none_instead_of_propagating(session):
    track_id = _make_track_record(session)
    sync = _RaisingSyncProvider(available={"song a": "sync-id"})
    auth_manager = FakeAuthManager()

    result = resolve_sync_id(session, track_id, as_proxy(sync), auth_manager, store=None)

    assert result is None
    assert _identifier_rows(session, track_id, sync_id_type(sync.name)) == []
