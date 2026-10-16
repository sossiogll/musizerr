"""
The data cleanup of fix/track-identity (``scripts/track_identity_cleanup.py``):
each step does what the report says and a second run finds nothing to do.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from core.identifiers import sync_id_type
from musizerr_sdk.models import LibraryItem
from db.models import (
    Download,
    DuplicateGroup,
    DuplicateMember,
    TrackIdentifierRecord,
    TrackRecord,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import stored_track_data
from scripts import track_identity_cleanup as cleanup

from tests.database import new_engine, seed_parents
from tests.conftest import make_track

SYNC = "fakesync"


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        yield s


def _record(session, title, *ids, duration_ms=246_000, album="D'ici et d'ailleurs") -> str:
    track = make_track(title, "Soha", album, duration_ms=duration_ms, year=2007)
    record = TrackRecord(title=title, artist="Soha", album=album, duration_ms=duration_ms,
                         track_data=stored_track_data(track))
    session.add(record)
    session.commit()
    for id_type, value in ids:
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=id_type, value=value))
    session.commit()
    return record.internal_id


def _item(sync_id, title="Mil pasos") -> LibraryItem:
    return LibraryItem(sync_id=sync_id, path=f"Soha/{title}.ogg", title=title, artist="Soha",
                       album="D'ici et d'ailleurs", year=2007, duration_ms=246_000, size=1, suffix="ogg",
                       bitrate=299_000, sample_rate=44_100)


def test_stale_sync_ids_are_refreshed_and_twins_merged(session):
    live = {"NEW": _item("NEW")}
    library = _record(session, "Mil pasos (feat. Antoine Essertier)", ("isrc", "FRZ1"), (sync_id_type(SYNC), "OLD"))
    session.add(Download(track_id=library, status="downloaded", file_path="Soha/mil.ogg"))
    liked = _record(session, "Mil pasos", (f"{SYNC}_id", "OLD"), (f"{SYNC}_id", "NEW"))
    seed_parents(session, users=["u1"])
    saved = UserSavedTrack(user_id="u1", track_id=liked)
    session.add(saved)
    session.flush()
    session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider=SYNC, provider_track_id="NEW"))
    session.commit()

    stale = cleanup.refresh_stale_sync_ids(session, SYNC, live)
    twins = cleanup.merge_twins(session, SYNC)
    aliases = cleanup.add_sync_aliases(session, SYNC, live)

    assert stale["refreshed"] == 1 and stale["removed_with_live_sibling"] == 1
    assert twins["merged"] == 1
    assert session.get(TrackRecord, liked) is None
    ids = {(r.id_type, r.value) for r in session.exec(select(TrackIdentifierRecord)).all()}
    assert ids == {("isrc", "FRZ1"), (sync_id_type(SYNC), "NEW"), (f"{SYNC}_id", "NEW")}
    assert session.exec(select(UserSavedTrack)).one().track_id == library
    assert aliases == 0

    # Idempotent.
    assert cleanup.refresh_stale_sync_ids(session, SYNC, live)["stale"] == 0
    assert cleanup.merge_twins(session, SYNC)["merged"] == 0
    assert cleanup.add_sync_aliases(session, SYNC, live) == 0


def test_twins_of_different_versions_are_not_merged(session):
    remaster = _record(session, "E si fa bello per te - Remastered 2023", (sync_id_type(SYNC), "S1"), duration_ms=194_000)
    remix = _record(session, "E si fa bello per te (Federico Scavo Remix)", (f"{SYNC}_id", "S1"), duration_ms=254_000)
    session.add(Download(track_id=remaster, status="downloaded", file_path="a.ogg"))
    session.commit()

    report = cleanup.merge_twins(session, SYNC)

    assert report == {"merged": 0, "incompatible": 1, "examples": []}
    assert session.get(TrackRecord, remix) is not None
    assert not session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == remix)).all()


def test_the_provider_id_of_the_sync_provider_gets_its_sync_id(session):
    liked = _record(session, "Mil pasos", (f"{SYNC}_id", "NEW"))

    assert cleanup.add_sync_aliases(session, SYNC, {"NEW": _item("NEW")}) == 1
    assert session.exec(select(TrackIdentifierRecord).where(
        TrackIdentifierRecord.track_id == liked, TrackIdentifierRecord.id_type == sync_id_type(SYNC))).one().value == "NEW"


def _group(session, status, *, deleted="[]", keepers=2, bitrate=320, origin="unknown", download_id=None):
    group = DuplicateGroup(recording_id="rec", status=status, reason="best_quality", deleted_paths=deleted)
    session.add(group)
    session.flush()
    for index, sync_id in enumerate(("sid-1", "sid-2")):
        session.add(DuplicateMember(group_id=group.id, sync_id=sync_id, path="A/01-04 - La fine.mp3", title="La fine",
                                    artist="Tiziano Ferro", suffix="mp3", bitrate=bitrate, sample_rate=44_100,
                                    lossless=False, quality_known=True, origin=origin, download_id=download_id,
                                    is_keeper=index < keepers))
    session.commit()
    return group.id


def test_bitrates_saved_in_kbps_become_bit_per_second(session):
    _group(session, "proposed")

    assert cleanup.fix_bitrates(session) == 2
    assert {m.bitrate for m in session.exec(select(DuplicateMember)).all()} == {320_000}
    assert cleanup.fix_bitrates(session) == 0


def test_a_group_resolved_with_phantom_keepers_is_reopened_and_recomputed(session):
    group_id = _group(session, "resolved")

    report = cleanup.fix_duplicate_groups(session, prefer_lossless=True)

    group = session.get(DuplicateGroup, group_id)
    assert report["reopened"] == [group_id]
    assert group.status == "needs_review"
    assert not any(m.is_keeper for m in session.exec(select(DuplicateMember)).all())
    assert cleanup.fix_duplicate_groups(session, prefer_lossless=True)["reopened"] == []


def test_a_wrongly_resolved_group_with_an_active_twin_is_superseded(session):
    resolved = _group(session, "resolved")
    active = _group(session, "proposed")

    report = cleanup.fix_duplicate_groups(session, prefer_lossless=True)

    assert report["superseded"] == [resolved]
    assert session.get(DuplicateGroup, resolved).status == "dismissed"
    assert session.get(DuplicateGroup, active).status == "needs_review"


def test_a_group_resolved_by_hand_with_one_keeper_stays_resolved(session):
    group_id = _group(session, "resolved", keepers=1)

    cleanup.fix_duplicate_groups(session, prefer_lossless=True)

    assert session.get(DuplicateGroup, group_id).status == "resolved"


class _Lookup:
    def __init__(self, tracks):
        self.tracks = tracks

    def track(self, id_type, value):
        return self.tracks.get((id_type, value))


def test_a_wrong_merge_is_split_and_the_like_follows(session):
    from tests.conftest import FakeRegistry

    record = _record(session, "E si fa bello per te - Remastered 2023", ("catalog_id", "remaster"),
                     ("catalog_id", "remix"), duration_ms=194_000, album="E si fa bello per te (i remix)")
    seed_parents(session, users=["u1"])
    saved = UserSavedTrack(user_id="u1", track_id=record, library_track_id=record)
    session.add(saved)
    session.flush()
    session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider="catalog", provider_track_id="remix"))
    session.add(UserFavorite(user_id="u1", track_id=record, source="match"))
    session.commit()
    remix = make_track("E si fa bello per te - Federico Scavo Remix", "Jovanotti", "E si fa bello per te (i remix)",
                       duration_ms=254_995, provider="catalog", provider_id="remix")
    from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
    remix.add_id(Identifier(type=PROVIDER_ID, value="remix", provider="catalog"))
    remaster = make_track("E si fa bello per te - Remastered 2023", "Jovanotti", duration_ms=194_040)
    lookup = _Lookup({("catalog_id", "remix"): remix, ("catalog_id", "remaster"): remaster})

    report = cleanup.split_wrong_merges(session, lookup, FakeRegistry())

    assert report["identifiers_split"] == 1 and report["likes_moved"] == 1
    ids = {r.value: r.track_id for r in session.exec(select(TrackIdentifierRecord)).all()}
    assert ids["remaster"] == record and ids["remix"] != record
    moved = session.exec(select(UserSavedTrack)).one()
    assert moved.track_id == ids["remix"] and moved.matched_at is None
    assert session.exec(select(UserFavorite)).all() == []
    assert cleanup.split_wrong_merges(session, lookup, FakeRegistry())["identifiers_split"] == 0


def test_a_stale_id_with_no_live_match_is_kept_rather_than_leaving_the_library(session):
    record = _record(session, "Gone Song", (sync_id_type(SYNC), "DEAD"))

    report = cleanup.refresh_stale_sync_ids(session, SYNC, {"NEW": _item("NEW")})

    assert report["kept_no_live_match"] == 1
    assert session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == record)).one().value == "DEAD"


def test_a_rate_limited_provider_is_benched_and_its_ids_left_alone(session, monkeypatch):
    import httpx

    from tests.conftest import FakeMetadataProvider, FakeRegistry

    class _Limited(FakeMetadataProvider):
        calls = 0

        def get_track(self, provider_id, session):
            _Limited.calls += 1
            request = httpx.Request("GET", "http://catalog.test")
            raise httpx.HTTPStatusError("429", request=request,
                                        response=httpx.Response(429, headers={"retry-after": "0"}, request=request))

    monkeypatch.setattr(cleanup.time, "sleep", lambda seconds: None)
    catalog = _Limited(name="catalog")
    registry = FakeRegistry(metadata=[catalog])
    record = _record(session, "Song", ("catalog_id", "a"), ("catalog_id", "b"), ("catalog_id", "c"))
    lookup = cleanup.Lookup(registry, _NoAuth(), {}, set(), [])

    report = cleanup.split_wrong_merges(session, lookup, registry)

    assert report["identifiers_split"] == 0
    assert lookup.benched == {"catalog"}
    assert _Limited.calls == 3  # one call and two retries, then nothing more
    assert lookup.not_verified["catalog_id"] == 3
    assert len(session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == record)).all()) == 3


class _NoAuth:
    def authenticate(self, provider):
        return None
