"""
``GET /downloads?phase=failed`` never lists a track that is in the library.

Seen live (2 Oct), right after a restart: the failed phase listed five
tracks with ``in_library: true`` / ``file_state: downloaded`` (Mr.
Brightside, Piccola Stella...), not reproduced later. Cause: the route
filters by phase in one query and computes each item's state (and the
track's ``in_library``) in later ones, and with the SQLite driver every
SELECT ran in its own implicit transaction. The sync library indexing
(``services.sync_index``, the first step of the like matching after a
restart) committed the tracks' sync ids between those queries: the filter
saw them failed, the state saw them in the library.

Now the route reads from one snapshot (``db.engine.read_snapshot``).
Reproduced here with a database of real connections (a WAL file on SQLite)
and a commit from another connection right after the phase filter ran.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlmodel import Session

from core import download_states as ds
from core.identifiers import sync_id_type
from db.models import Download, TrackIdentifierRecord, TrackRecord, User
from routes import history
from services.capabilities import Viewer
from tests.conftest import FakeRegistry, FakeSyncProvider
from tests.database import new_engine

SYNC = "nav"


@pytest.fixture
def engine():
    # Real connections, not the shared in-memory one: the other commit comes
    # from a connection of its own.
    return new_engine(addressable=True)


def test_the_failed_phase_and_the_item_states_come_from_one_snapshot(engine, monkeypatch):
    monkeypatch.setattr(history, "enrich_dicts_with_stream_url", lambda *a, **k: None)
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        track = TrackRecord(title="Mr. Brightside", artist="The Killers", album="Hot Fuss")
        session.add_all([admin, track])
        session.commit()
        session.add(Download(track_id=track.internal_id, status=ds.FAILED, retriable=False))
        session.commit()
        track_id = track.internal_id
        session.refresh(admin)

    indexed = []

    @event.listens_for(engine, "after_cursor_execute")
    def _index_meanwhile(conn, cursor, statement, parameters, context, executemany):
        # The indexing job commits the sync id right after the phase filter.
        if not indexed and "last_activity" in statement:
            indexed.append(True)
            with Session(engine) as other:
                other.add(TrackIdentifierRecord(track_id=track_id, id_type=sync_id_type(SYNC), value="sid-1"))
                other.commit()

    with Session(engine) as session:
        page = history.get_downloads(
            session, registry, auth_manager=object(), user=admin, viewer=Viewer(user_id=admin.id, stream_full=True),
            page=1, limit=50, status=None, q=None, sort="recent", phase=["failed"],
        )

    assert indexed
    for item in page["items"]:
        assert ds.phase_of(item["status"]) == "failed", item["status"]
        assert item["track"].get("in_library") is not True
