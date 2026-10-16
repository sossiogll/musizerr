"""Copre `services/track_state.py::annotate_tracks`: lo stato file
(`download_status`) mostrato accanto alle tracce nelle liste (ricerca,
discover, album, preferiti, ...), deciso dal server.

Stile in linea con `test_history_permissions.py`: DB SQLite in-memory,
funzione chiamata direttamente (nessun TestClient).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from db.models import Download, TrackIdentifierRecord, TrackRecord
from services.capabilities import Viewer
from services.track_state import annotate_tracks
from tests.conftest import FakeRegistry

GUEST = Viewer(user_id=None, stream_full=False)


def annotate_download_status(tracks, session):
    annotate_tracks(tracks, session, GUEST, FakeRegistry())


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _track_dict(**overrides) -> dict:
    base = {
        "internal_id": "ephemeral",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "duration_ms": 200_000,
        "provider": "fake",
        "provider_id": "fake-1",
        "external_ids": [],
        "providers": ["fake"],
    }
    base.update(overrides)
    return base


def test_never_requested_is_none(session):
    track = _track_dict()
    annotate_download_status([track], session)
    assert track["download_status"] is None


def test_resolves_via_native_provider_id(session):
    record = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(record)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="fake_id", value="fake-1"))
    session.add(Download(track_id=record.internal_id, status="downloading"))
    session.commit()

    track = _track_dict(provider="fake", provider_id="fake-1")
    annotate_download_status([track], session)

    assert track["download_status"] == "downloading"


def test_resolves_via_isrc_external_id(session):
    record = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(record)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="isrc", value="GBAYE9200715"))
    session.add(Download(track_id=record.internal_id, status="downloaded"))
    session.commit()

    # provider_id nativo diverso da quanto salvato: deve comunque risolvere via ISRC.
    track = _track_dict(
        provider="other",
        provider_id="other-9",
        external_ids=[{"type": "isrc", "value": "GBAYE9200715"}],
    )
    annotate_download_status([track], session)

    assert track["download_status"] == "downloaded"


def test_no_matching_track_record_is_none(session):
    track = _track_dict(provider="ghost", provider_id="nope", external_ids=[{"type": "isrc", "value": "ZZZ"}])
    annotate_download_status([track], session)
    assert track["download_status"] is None


def test_matched_track_with_no_download_rows_is_none(session):
    record = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(record)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="fake_id", value="fake-1"))
    session.commit()

    track = _track_dict(provider="fake", provider_id="fake-1")
    annotate_download_status([track], session)

    assert track["download_status"] is None


def test_aggregates_multiple_attempts_most_advanced_wins(session):
    record = TrackRecord(title="Song", artist="Artist", album="Album")
    session.add(record)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="fake_id", value="fake-1"))
    session.add(Download(track_id=record.internal_id, status="failed"))
    session.add(Download(track_id=record.internal_id, status="downloaded"))
    session.commit()

    track = _track_dict(provider="fake", provider_id="fake-1")
    annotate_download_status([track], session)

    # "downloaded" precede "failed" nell'ordine canonico (core.download_states.ORDER).
    assert track["download_status"] == "downloaded"


def test_batches_multiple_tracks_in_one_call(session):
    r1 = TrackRecord(title="Song 1", artist="Artist", album="Album")
    r2 = TrackRecord(title="Song 2", artist="Artist", album="Album")
    session.add(r1)
    session.add(r2)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=r1.internal_id, id_type="fake_id", value="fake-1"))
    session.add(TrackIdentifierRecord(track_id=r2.internal_id, id_type="fake_id", value="fake-2"))
    session.add(Download(track_id=r1.internal_id, status="pending"))
    session.commit()

    t1 = _track_dict(provider="fake", provider_id="fake-1")
    t2 = _track_dict(provider="fake", provider_id="fake-2")
    t3 = _track_dict(provider="fake", provider_id="fake-3")  # mai vista dal DB
    annotate_download_status([t1, t2, t3], session)

    assert t1["download_status"] == "pending"
    assert t2["download_status"] is None  # ha un TrackRecord ma nessun Download
    assert t3["download_status"] is None  # nessun match affatto


def test_empty_list_is_a_noop(session):
    annotate_download_status([], session)  # non deve sollevare
