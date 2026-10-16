"""
Which library record a track is, decided once and the same way everywhere:

- a provider that is also the sync provider has ONE identity space: its
  provider id and its sync id are the same value, so the like imported from
  it and the library record indexed by the stream enrichment are one record;
- the resolution is deterministic: universal ids (ISRC/MBID) win over
  provider ids, which win over sync ids; identifiers of the same rank that
  point to different records are "ambiguous" (no record is claimed), in
  every process whatever its ``PYTHONHASHSEED``;
- a sync provider that returns a new id for a record replaces the stale one
  (written by a write path or the indexing job, never by a read);
- the availability on the sync provider is decided by the identifiers a
  row carries, not by whether it already has some stream URL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from sqlmodel import Session, select

from core.identifiers import AMBIGUOUS, resolve_identity, sync_id_type
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from core.serialization import serialize_track
from db.models import TrackIdentifierRecord, TrackRecord
from db.track_store import find_or_create_track, stored_track_data
from services.capabilities import Viewer
from services.enrichment import enrich_dicts_with_stream_url
from services.track_identity import with_sync_aliases
from services.track_state import annotate_tracks, known_track_ids

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track

SYNC = "fakesync"
SYNC_TYPE = sync_id_type(SYNC)
PROVIDER_TYPE = f"{SYNC}_id"
VIEWER = Viewer(user_id=None, stream_full=True)


def _record(session, title, *ids, artist="Soha", album="D'ici et d'ailleurs") -> str:
    track = make_track(title, artist, album, year=2007, duration_ms=246_000)
    record = TrackRecord(title=title, artist=artist, album=album, duration_ms=246_000, track_data=stored_track_data(track))
    session.add(record)
    session.commit()
    for id_type, value in ids:
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=id_type, value=value))
    session.commit()
    return record.internal_id


def _sync_row(title, sync_value, provider=SYNC):
    track = make_track(title, "Soha", "D'ici et d'ailleurs", provider=provider, provider_id=sync_value, year=2007,
                       duration_ms=246_000)
    track.add_id(Identifier(type=PROVIDER_ID, value=sync_value, provider=provider))
    return track


# ---------------------------------------------------------------------------
# One identity space for the sync provider
# ---------------------------------------------------------------------------


def test_the_like_from_the_sync_provider_is_the_library_record(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        library = _record(session, "Mil pasos (feat. Antoine Essertier)", (SYNC_TYPE, "3LeLAPhNgX47aac7HOFMSw"))

        liked = with_sync_aliases(_sync_row("Mil pasos", "3LeLAPhNgX47aac7HOFMSw"), registry)
        record = find_or_create_track(liked, session)

        assert record.internal_id == library
        assert len(session.exec(select(TrackRecord)).all()) == 1


def test_a_row_of_the_sync_provider_is_in_the_library(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        library = _record(session, "Mil pasos", (SYNC_TYPE, "3LeLAPhNgX47aac7HOFMSw"))
        rows = [serialize_track(_sync_row("Mil pasos", "3LeLAPhNgX47aac7HOFMSw"))]

        annotate_tracks(rows, session, VIEWER, registry)

        assert rows[0]["library_id"] == library
        assert rows[0]["in_library"] is True


def test_a_provider_that_is_not_a_sync_provider_keeps_its_own_space(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    other = make_track("Mil pasos", "Soha", provider="catalog", provider_id="3LeLAPhNgX47aac7HOFMSw")
    other.add_id(Identifier(type=PROVIDER_ID, value="3LeLAPhNgX47aac7HOFMSw", provider="catalog"))

    assert {i.storage_type() for i in with_sync_aliases(other, registry).external_ids} == {"catalog_id"}


# ---------------------------------------------------------------------------
# Deterministic resolution
# ---------------------------------------------------------------------------


def test_the_rank_of_an_identifier_decides():
    assert resolve_identity([("catalog_id", "B"), ("isrc", "A")]) == "A"
    assert resolve_identity([(SYNC_TYPE, "C"), ("catalog_id", "B")]) == "B"
    assert resolve_identity([("isrc", "A"), ("isrc", "B")]) == AMBIGUOUS
    assert resolve_identity([("isrc", "A"), ("mbid", "A"), ("catalog_id", "B")]) == "A"
    assert resolve_identity([]) is None


_SCRIPT = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, ".")
    from sqlmodel import Session
    from db.models import TrackIdentifierRecord, TrackRecord
    from services.track_state import known_track_ids
    from tests.database import new_engine, release_engines

    engine = new_engine()
    with Session(engine) as session:
        for internal_id, ids in (("A", [("isrc", "I1"), ("other_id", "d1")]),
                                 ("B", [("isrc", "I2"), ("catalog_id", "s1"), ("fakesync_sync_id", "n1")]),
                                 ("C", [("catalog_id", "s2")])):
            session.add(TrackRecord(internal_id=internal_id, title="Song", artist="Band", album="Record"))
            session.flush()
            for id_type, value in ids:
                session.add(TrackIdentifierRecord(track_id=internal_id, id_type=id_type, value=value))
        session.commit()
        rows = [
            # ISRC -> A, catalog id -> B: the ISRC wins.
            {"provider": "catalog", "provider_id": "s1", "external_ids": [{"type": "isrc", "value": "I1"}]},
            # Two ISRCs of two records: ambiguous.
            {"provider": "x", "provider_id": "x1",
             "external_ids": [{"type": "isrc", "value": "I1"}, {"type": "isrc", "value": "I2"}]},
            # A provider id -> C, a sync id -> B: the provider id wins.
            {"provider": "catalog", "provider_id": "s2", "external_ids": [{"type": "fakesync_sync_id", "value": "n1"}]},
            # Two provider ids of two records: ambiguous.
            {"provider": "other", "provider_id": "d1", "external_ids": [{"type": "catalog_id", "value": "s2"}]},
        ]
        print(json.dumps(known_track_ids(rows, session)))
    release_engines()
    """
)


def test_the_resolution_is_the_same_in_every_process():
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outputs = set()
    for seed in ("0", "1", "2", "3", "7", "42", "1234", "99999"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run([sys.executable, "-c", _SCRIPT], cwd=app_dir, env=env, capture_output=True, text=True,
                                check=True)
        outputs.add(result.stdout.strip().splitlines()[-1])

    assert len(outputs) == 1
    assert json.loads(outputs.pop()) == ["A", None, "C", None]


# ---------------------------------------------------------------------------
# Stale sync ids
# ---------------------------------------------------------------------------


def test_a_new_sync_id_replaces_the_stale_one(engine):
    with Session(engine) as session:
        library = _record(session, "Mil pasos", ("isrc", "FRZ110701478"), (SYNC_TYPE, "MLcnZrTWm5Gra37tfUqBLB"))
        row = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", isrc="FRZ110701478", provider="catalog",
                         provider_id="c1")
        row.add_id(Identifier(type=SYNC_TYPE, value="3LeLAPhNgX47aac7HOFMSw"))

        find_or_create_track(row, session)

        stored = session.exec(select(TrackIdentifierRecord.value).where(
            TrackIdentifierRecord.track_id == library, TrackIdentifierRecord.id_type == SYNC_TYPE)).all()
        assert stored == ["3LeLAPhNgX47aac7HOFMSw"]
        assert known_track_ids([{"provider": "x", "provider_id": "y",
                                 "external_ids": [{"type": SYNC_TYPE, "value": "MLcnZrTWm5Gra37tfUqBLB"}]}],
                               session) == [None]


def test_reading_a_row_with_a_new_sync_id_does_not_replace_the_stored_one(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        library = _record(session, "Mil pasos", ("isrc", "FRZ110701478"), (SYNC_TYPE, "MLcnZrTWm5Gra37tfUqBLB"))
        row = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", isrc="FRZ110701478", provider="catalog",
                         provider_id="c1")
        row.add_id(Identifier(type=SYNC_TYPE, value="3LeLAPhNgX47aac7HOFMSw"))
        rows = [serialize_track(row)]

        annotate_tracks(rows, session, VIEWER, registry)

        assert rows[0]["library_id"] == library
        stored = session.exec(select(TrackIdentifierRecord.value).where(
            TrackIdentifierRecord.track_id == library, TrackIdentifierRecord.id_type == SYNC_TYPE)).all()
        assert stored == ["MLcnZrTWm5Gra37tfUqBLB"]


# ---------------------------------------------------------------------------
# Availability by identifiers
# ---------------------------------------------------------------------------


def test_a_row_with_a_stored_stream_url_is_still_checked_on_the_sync_provider():
    sync = FakeSyncProvider(SYNC, available={"mil pasos": "3LeLAPhNgX47aac7HOFMSw"})
    rows = [{**serialize_track(make_track("Mil pasos", "Soha", provider="elsewhere", provider_id="e1")),
             "stream_url": "hostref:/stream/provider/elsewhere/e1"}]

    enrich_dicts_with_stream_url(rows, FakeRegistry(syncs=[sync]), FakeAuthManager())

    assert sync.availability_calls == 1
    assert {"type": SYNC_TYPE, "value": "3LeLAPhNgX47aac7HOFMSw"} in rows[0]["external_ids"]


def test_a_row_that_carries_its_sync_id_needs_no_lookup():
    sync = FakeSyncProvider(SYNC, available={"mil pasos": "other"})
    native = serialize_track(_sync_row("Mil pasos", "3LeLAPhNgX47aac7HOFMSw"))
    native["stream_url"] = None

    enrich_dicts_with_stream_url([native], FakeRegistry(syncs=[sync]), FakeAuthManager())

    assert sync.availability_calls == 0
    assert {"type": SYNC_TYPE, "value": "3LeLAPhNgX47aac7HOFMSw"} in native["external_ids"]


def test_a_row_without_shared_ids_finds_its_record_by_the_core_matching(engine):
    """Seen live: the enrichment by one catalog of the album page did not answer, the
    remaster row came with only the id of another catalog and the current sync id, and
    the exact-artist fallback ("Jovanotti" vs "Jovanotti, Ackeejuice
    Rockers") made a twin of the library record. The resolution that WRITES
    (an import, the indexing job) goes through the core matching."""
    with Session(engine) as session:
        track = make_track("E si fa bello per te - Remastered 2023", "Jovanotti, Ackeejuice Rockers",
                           "E si fa bello per te (i remix)", duration_ms=194_040, year=2023)
        record = TrackRecord(title=track.title, artist=track.artist, album=track.album, duration_ms=194_040,
                             track_data=stored_track_data(track))
        session.add(record)
        session.commit()
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=SYNC_TYPE, value="ENTFKhtqleC1FwWlhBHFMX"))
        session.commit()
        row = make_track("E si fa bello per te (Remastered 2023)", "Jovanotti", "E si fa bello per te (i remix)",
                         duration_ms=194_000, year=2023, provider="catalog", provider_id="2195472647")
        row.add_id(Identifier(type=PROVIDER_ID, value="2195472647", provider="catalog"))
        row.add_id(Identifier(type=SYNC_TYPE, value="6K9rzfvHv4m4wN5WA8B8Ze"))

        found = find_or_create_track(row, session)

        assert found.internal_id == record.internal_id
        assert len(session.exec(select(TrackRecord)).all()) == 1
        sync_ids = session.exec(select(TrackIdentifierRecord.value).where(TrackIdentifierRecord.id_type == SYNC_TYPE)).all()
        assert sync_ids == ["6K9rzfvHv4m4wN5WA8B8Ze"]


def test_reading_that_row_makes_no_twin_and_writes_nothing(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        track = make_track("E si fa bello per te - Remastered 2023", "Jovanotti, Ackeejuice Rockers",
                           "E si fa bello per te (i remix)", duration_ms=194_040, year=2023)
        record = TrackRecord(title=track.title, artist=track.artist, album=track.album, duration_ms=194_040,
                             track_data=stored_track_data(track))
        session.add(record)
        session.commit()
        row = make_track("E si fa bello per te (Remastered 2023)", "Jovanotti", "E si fa bello per te (i remix)",
                         duration_ms=194_000, year=2023, provider="catalog", provider_id="2195472647")
        row.add_id(Identifier(type=SYNC_TYPE, value="6K9rzfvHv4m4wN5WA8B8Ze"))
        rows = [serialize_track(row)]

        annotate_tracks(rows, session, VIEWER, registry)

        assert rows[0]["library_id"] is None
        assert len(session.exec(select(TrackRecord)).all()) == 1
        assert session.exec(select(TrackIdentifierRecord)).all() == []
