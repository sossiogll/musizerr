"""
Library duplicates without phantom keepers (analysis of 29 Sep):

- the path a sync server reports can be virtual (rebuilt from the tags): two
  physical files can share it. Members and keepers are keyed by their sync
  id, so two files with one virtual path are two members, one keeper at most,
  and a group where nothing can be deleted is ``needs_review``, never a
  "proposed" deletion of zero files;
- when two members claim one Musizeer download, an exact link (by sync id)
  wins over a tag fallback: the file Musizeer downloaded stays deletable;
  both claims are dropped only when they have the same strength;
- the bit rate of a library item is in bit/s (a provider whose server speaks
  kbps converts it; the conversion is tested in its own repository);
- different versions of one song (remixes) are not duplicate candidates.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from musizeer_sdk.auth import AuthSession
from core.audio_quality import QualityScore
from core.duplicates import cluster_library_items, quality_from_library_item
from core.identifiers import sync_id_type
from db.duplicate_store import MemberInput, upsert_group_for_recording
from db.models import Download, DuplicateGroup, DuplicateMember, TrackIdentifierRecord, TrackRecord
from services.config_manager import ConfigManager
from workers import duplicates

from tests.conftest import FakeIdentificationProvider, FakeRegistry, FakeSyncProvider, encode_identity_bytes, make_library_item
from tests.database import new_engine, seed_parents


def _twin_files():
    """Two physical files a sync server lists under ONE virtual path."""
    return [
        make_library_item("Closing Time", "Some Band", "Their Record", sync_id=sync_id, path="Some Band/Their Record/04 - Closing Time.mp3",
                          suffix="mp3", bitrate=320_000, size=size, duration_ms=226_000, year=2011)
        for sync_id, size in (("sid-a", 9_380_360), ("sid-b", 9_383_217))
    ]


# ---------------------------------------------------------------------------
# Bit/s, end to end
# ---------------------------------------------------------------------------


def test_the_bitrate_of_a_library_item_reaches_its_quality_in_bit_per_second():
    items = _twin_files()

    assert {quality_from_library_item(item).bitrate for item in items} == {320_000}


def test_a_group_member_stores_bit_per_second(engine):
    items = _twin_files()
    with Session(engine) as session:
        group, _ = upsert_group_for_recording(session, recording_id="rec", members=[_member(i) for i in items],
                                              prefer_lossless=True)
        session.commit()
        rows = session.exec(select(DuplicateMember)).all()

    # The UI divides by 1000 to show kbps: 320, never 0.
    assert {row.bitrate // 1000 for row in rows} == {320}


# ---------------------------------------------------------------------------
# One virtual path, two files
# ---------------------------------------------------------------------------


def _member(item, download_id=None) -> MemberInput:
    return MemberInput(
        sync_id=item.sync_id, path=item.path, title=item.title, artist=item.artist, album=item.album,
        year=item.year, duration_ms=item.duration_ms, size=item.size, suffix=item.suffix, bitrate=item.bitrate,
        sample_rate=item.sample_rate, quality=quality_from_library_item(item), fingerprint_score=0.9,
        download_id=download_id,
    )


def test_two_files_with_one_virtual_path_and_nothing_deletable_need_review(engine):
    items = _twin_files()
    assert len({item.path for item in items}) == 1 and len(items) == 2

    with Session(engine) as session:
        group, _ = upsert_group_for_recording(session, recording_id="rec", members=[_member(i) for i in items],
                                              prefer_lossless=True)
        session.commit()
        keepers = session.exec(select(DuplicateMember).where(DuplicateMember.is_keeper)).all()

        assert group.status == "needs_review"
        assert keepers == []


def test_two_files_with_one_virtual_path_get_one_keeper(engine):
    items = _twin_files()
    with Session(engine) as session:
        seed_parents(session, tracks=["t"])
        download = Download(track_id="t", status="downloaded", file_path="real/closing-time.mp3")
        session.add(download)
        session.commit()
        # Exactly one member is the keeper; the other one, Musizeer's, is the
        # one to delete.
        members = [_member(items[0]), _member(items[1], download_id=download.id)]
        members[0].quality = QualityScore(lossless=False, bitrate=320_000, sample_rate=44_100, codec="mp3")
        members[1].quality = QualityScore(lossless=False, bitrate=256_000, sample_rate=44_100, codec="mp3")
        group, _ = upsert_group_for_recording(session, recording_id="rec", members=members, prefer_lossless=True)
        session.commit()
        status = group.status
        rows = session.exec(select(DuplicateMember)).all()

    assert status == "proposed"
    assert [row.sync_id for row in rows if row.is_keeper] == [items[0].sync_id]
    assert [row.sync_id for row in rows if not row.is_keeper and row.download_id] == [items[1].sync_id]


# ---------------------------------------------------------------------------
# Exact link against tag fallback
# ---------------------------------------------------------------------------


class _AnonymousAuth:
    def __init__(self, credential_store=None) -> None:
        pass

    def authenticate(self, provider):
        return AuthSession.anonymous()


@pytest.fixture
def scan(monkeypatch):
    engine = new_engine()
    monkeypatch.setattr(duplicates, "engine", engine)
    monkeypatch.setattr(duplicates, "auth_manager_for", lambda store: _AnonymousAuth(store))
    settings_engine = new_engine()
    config = ConfigManager(engine=settings_engine)
    config.update({"dedup_enabled": True, "identification_enabled": True, "sync_provider": "fakesync",
                   "storage_provider": "fakestorage", "dedup_prefer_lossless": True})
    monkeypatch.setattr(duplicates, "get_config", lambda: config)

    def run(sync):
        registry = FakeRegistry(syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)])
        monkeypatch.setattr(duplicates, "get_registry", lambda: registry)
        return duplicates.scan_library_duplicates()

    return engine, run


def test_an_exact_link_beats_a_tag_fallback_on_the_same_download(scan):
    engine, run = scan
    # "Wastelands": the file Musizeer downloaded (linked by its sync id) and
    # an older copy whose tags also match that download.
    ours = make_library_item("Wastelands", "Linkin Park", "The Hunting Party", sync_id="sid-ours", path="a.flac",
                             suffix="mp3", bitrate=192_000, size=1000, duration_ms=215_000)
    theirs = make_library_item("Wastelands", "Linkin Park", "The Hunting Party", sync_id="sid-theirs", path="b.flac",
                               suffix="flac", bitrate=900_000, size=2000, duration_ms=215_000)
    sync = FakeSyncProvider("fakesync", library=[ours, theirs])
    sync.files["sid-ours"] = encode_identity_bytes("rec-w")
    sync.files["sid-theirs"] = encode_identity_bytes("rec-w")
    with Session(engine) as session:
        record = TrackRecord(title="Wastelands", artist="Linkin Park", album="The Hunting Party", duration_ms=215_000)
        session.add(record)
        session.flush()
        session.add(Download(track_id=record.internal_id, status="downloaded", file_path="real/wastelands.mp3"))
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=sync_id_type("fakesync"), value="sid-ours"))
        session.commit()

    run(sync)

    with Session(engine) as session:
        group = session.exec(select(DuplicateGroup)).one()
        by_sync = {m.sync_id: m for m in session.exec(select(DuplicateMember)).all()}
    assert by_sync["sid-ours"].download_id is not None
    assert by_sync["sid-theirs"].download_id is None
    assert group.status == "proposed"
    assert by_sync["sid-theirs"].is_keeper


# ---------------------------------------------------------------------------
# Versions are not duplicates
# ---------------------------------------------------------------------------


def test_remixes_of_one_song_are_not_duplicate_candidates():
    versions = [
        ("Anthem", 260), ("Anthem (Country Road version)", 261), ("Anthem (Radio mix)", 231), ("Anthem (Some Producer remix)", 358),
    ]
    born = [
        make_library_item(title, "Some Artist", "Their Record", sync_id=f"sid-{n}", path=f"Some Artist/Their Record/{title}.flac",
                          suffix="flac", bitrate=1_000_000, size=30_000_000 + n, duration_ms=seconds * 1000, year=2011)
        for n, (title, seconds) in enumerate(versions)
    ]

    assert cluster_library_items(born) == []


def test_two_copies_of_one_version_are_duplicate_candidates():
    # The control of the test above: the same title and length IS a candidate.
    copies = [
        make_library_item("Anthem", "Some Artist", "Their Record", sync_id=f"sid-{n}", path=f"Some Artist/Their Record/{n}/Anthem.flac",
                          suffix="flac", bitrate=1_000_000, size=30_000_000, duration_ms=260_000, year=2011)
        for n in range(2)
    ]

    assert len(cluster_library_items(copies)) == 1
