"""
Library file reconciliation (``services.library_reconcile``): library entries
that pointed at files the storage had lost (renamed or moved by a later
naming fix or by an external tool) kept their tracks "in library" with full
playback while ``/stream`` answered 404. The reconciliation finds the file
again (current naming template, the same path ignoring case and
punctuation, or the sync provider's file by size) or marks the entry missing;
"in library" and playback then follow the files that really exist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from core.identifiers import sync_id_type
from musizerr_sdk.models import LibraryItem
from db.library_store import file_states, in_library, stored_file_paths
from db.models import Download, LibraryEntry, TrackIdentifierRecord
from db.track_store import find_or_create_track
from services.capabilities import Viewer
from services.library_reconcile import (
    CANNOT_LIST,
    EMPTY_LISTING,
    LISTING_UNRELATED,
    SYNC_SIZE,
    TEMPLATE,
    TEMPLATE_NORMALIZED,
    reconcile_library_files,
)
from services.stored_tracks import public_track_dicts
from services.track_state import annotate_tracks
from db.models import TrackRecord
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeStorageProvider, FakeSyncProvider, make_track

FORMAT = "{artist}/{album} ({release_year})/{artist} - {album} - {track_number:02d} - {title}"
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class _ListedStorage(FakeStorageProvider):
    """A storage that lists its files and tells their size, counting both."""

    def __init__(self, files: dict[str, bytes] | None = None, *, listable: bool = True) -> None:
        super().__init__("shelf")
        self.files = dict(files or {})
        self.listable = listable
        self.size_calls: list[str] = []

    def list_files(self, prefix, session):
        return iter(sorted(self.files)) if self.listable else None

    def size(self, relative_path, session):
        self.size_calls.append(relative_path)
        data = self.files.get(relative_path)
        return len(data) if data is not None else None


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        yield s


def _config():
    return SimpleNamespace(storage_provider="shelf", track_format=FORMAT)


def _stored(session, path: str, *, title="Song", artist="Artist", album="Album", year=2020, number=1,
            provider_id=None) -> str:
    """A track that landed as ``path``: its download and its library entry."""
    track = make_track(title=title, artist=artist, album=album, year=year, provider_id=provider_id or title)
    track.track_number = number
    record = find_or_create_track(track, session)
    session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path=path, completed_at=NOW))
    session.add(LibraryEntry(track_id=record.internal_id, file_path=path, file_format=path.rsplit(".", 1)[-1],
                             storage_provider="shelf"))
    session.commit()
    return record.internal_id


def _reconcile(session, storage, *, apply=True, syncs=(), **kwargs):
    registry = FakeRegistry(storage=[storage], syncs=list(syncs))
    return reconcile_library_files(session, registry, _config(), FakeAuthManager(), apply=apply, now=NOW, **kwargs)


def _entry(session, track_id) -> LibraryEntry:
    return session.exec(select(LibraryEntry).where(LibraryEntry.track_id == track_id)).one()


def _download_paths(session, track_id) -> list[str]:
    return [d.file_path for d in session.exec(select(Download).where(Download.track_id == track_id)).all()]


KEPT = "K/K (2020)/K - K - 01 - Keep.ogg"


def _keeper(session) -> str:
    """A track whose file is where the library says: the listing is this
    library's."""
    return _stored(session, KEPT, title="Keep", artist="K", album="K")


OLD = "Artist/Album (Unknown)/Artist - Album - 00 - Song.ogg"
NEW = "Artist/Album (2020)/Artist - Album - 01 - Song.ogg"


# ---------------------------------------------------------------------------
# Finding a file again
# ---------------------------------------------------------------------------


def test_files_where_the_library_says_are_left_alone(session):
    track_id = _stored(session, NEW)
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage)

    assert (report.status, report.entries, report.present) == ("ok", 1, 1)
    assert report.missing_new == 0 and sum(report.relocated.values()) == 0
    assert _entry(session, track_id).missing_since is None


def test_a_file_renamed_by_the_current_template_is_found_again(session):
    track_id = _stored(session, OLD)
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage)

    assert report.relocated[TEMPLATE] == 1
    assert _entry(session, track_id).file_path == NEW
    assert _download_paths(session, track_id) == [NEW]
    assert stored_file_paths(session, track_id) == [NEW]


def test_a_dry_run_reports_and_changes_nothing(session):
    track_id = _stored(session, OLD)
    gone_id = _stored(session, "Other/Gone (Unknown)/Other - Gone - 00 - Gone.ogg", title="Gone", artist="Other",
                      album="Gone")
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage, apply=False)

    assert report.dry_run is True
    assert report.relocated[TEMPLATE] == 1 and report.missing_new == 1 and report.left_library == 1
    assert report.relocations == [
        {"track_id": track_id, "from": OLD, "to": NEW, "how": TEMPLATE, "superseded": False},
    ]
    assert _entry(session, track_id).file_path == OLD
    assert _entry(session, gone_id).missing_since is None
    assert _download_paths(session, track_id) == [OLD]


def test_a_file_renamed_with_other_case_and_punctuation_is_found_again(session):
    track_id = _stored(session, "Manu Chao/Esperanza (Unknown)/Manu Chao - Próxima Estación_ Esperanza - 00 - Me Gustas Tu.ogg",
                       title="Me Gustas Tu", artist="Manu Chao", album="Próxima Estación: Esperanza", year=2001,
                       number=6)
    renamed = "Manu Chao/Próxima estación - Esperanza (2001)/Manu Chao - Próxima estación - Esperanza - 06 - Me gustas tú.ogg"
    storage = _ListedStorage({renamed: b"x"})

    report = _reconcile(session, storage)

    assert report.relocated[TEMPLATE_NORMALIZED] == 1
    assert _entry(session, track_id).file_path == renamed


def _synced(session, track_id: str, sync_id: str) -> None:
    session.add(TrackIdentifierRecord(track_id=track_id, id_type=sync_id_type("fakesync"), value=sync_id))
    session.commit()


def _sync_with(*items: LibraryItem) -> FakeSyncProvider:
    return FakeSyncProvider(library=list(items))


def test_a_file_moved_by_an_external_tool_is_found_by_the_sync_providers_size(session):
    track_id = _stored(session, "Justice, Tame Impala/Hyperdrama (Unknown)/Justice, Tame Impala - Hyperdrama - 00 - Neverender.ogg",
                       title="Neverender", artist="Justice, Tame Impala", album="Hyperdrama", year=2024, number=1)
    _synced(session, track_id, "s1")
    moved = "Justice/Hyperdrama (2024)/Justice - Hyperdrama - 01 - Neverender.ogg"
    storage = _ListedStorage({
        moved: b"a" * 1000,
        "Other/Neverender (2010)/Other - Neverender - 01 - Neverender.ogg": b"b" * 900,  # same title, other size
        "Justice/Hyperdrama (2024)/Justice - Hyperdrama - 02 - Generator.ogg": b"c" * 1000,  # same size, other title
    })
    sync = _sync_with(LibraryItem(sync_id="s1", path="Justice/Hyperdrama/01-01 - Neverender.ogg", title="Neverender",
                                  artist="Justice", album="Hyperdrama", size=1000, suffix="ogg"))

    report = _reconcile(session, storage, syncs=[sync])

    assert report.relocated[SYNC_SIZE] == 1
    assert _entry(session, track_id).file_path == moved
    assert set(storage.size_calls) <= {moved, "Other/Neverender (2010)/Other - Neverender - 01 - Neverender.ogg"}


def test_two_files_of_the_same_title_and_size_are_not_guessed_between(session):
    track_id = _stored(session, OLD)
    _keeper(session)
    _synced(session, track_id, "s1")
    storage = _ListedStorage({KEPT: b"k", "X/One/X - 01 - Song.ogg": b"a" * 10, "Y/Two/Y - 03 - Song.ogg": b"b" * 10})
    sync = _sync_with(LibraryItem(sync_id="s1", path="v", title="Song", artist="X", size=10, suffix="ogg"))

    report = _reconcile(session, storage, syncs=[sync])

    assert sum(report.relocated.values()) == 0 and report.missing_new == 1
    assert _entry(session, track_id).missing_since is not None


def test_the_size_checks_have_a_budget(session):
    track_id = _stored(session, OLD)
    _keeper(session)
    _synced(session, track_id, "s1")
    storage = _ListedStorage({KEPT: b"k", **{f"F{i}/F - 01 - Song.ogg": b"a" * i for i in range(1, 6)}})
    sync = _sync_with(LibraryItem(sync_id="s1", path="v", title="Song", artist="X", size=5, suffix="ogg"))

    report = _reconcile(session, storage, syncs=[sync], max_size_checks=2)

    assert report.size_checks == 2 and len(storage.size_calls) == 2
    assert report.missing_new == 1


def test_a_file_that_is_another_tracks_entry_is_never_taken(session):
    track_id = _stored(session, OLD)
    other_id = _stored(session, NEW, title="Different", provider_id="different")
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage)

    assert sum(report.relocated.values()) == 0
    assert _entry(session, track_id).missing_since is not None
    assert _entry(session, other_id).file_path == NEW and _entry(session, other_id).missing_since is None


def test_a_file_already_known_for_the_same_track_supersedes_the_lost_entry(session):
    track_id = _stored(session, OLD)
    # The track was downloaded again later, under the new name.
    session.add(Download(track_id=track_id, status=ds.DOWNLOADED, file_path=NEW, completed_at=NOW))
    session.add(LibraryEntry(track_id=track_id, file_path=NEW, file_format="ogg", storage_provider="shelf"))
    session.commit()
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage)

    assert report.superseded == 1 and report.duplicates == []
    assert _download_paths(session, track_id) == [NEW, NEW]
    assert stored_file_paths(session, track_id) == [NEW]
    assert track_id in in_library(session, [track_id], [])


# ---------------------------------------------------------------------------
# Marking a file missing, and back
# ---------------------------------------------------------------------------


def test_a_lost_file_takes_the_track_out_of_the_library(session):
    track_id = _stored(session, OLD)
    keeper = _keeper(session)
    storage = _ListedStorage({KEPT: b"x"})

    report = _reconcile(session, storage)

    assert (report.missing_new, report.left_library) == (1, 1)
    assert _entry(session, track_id).missing_since is not None
    assert in_library(session, [track_id, keeper], []) == {keeper}
    assert stored_file_paths(session, track_id) == []
    # The landed attempt no longer says "downloaded": the track can be asked again.
    assert file_states(session, [track_id], [])[track_id] == ds.FAILED


def test_a_lost_file_with_a_sync_copy_keeps_the_track_in_the_library(session):
    track_id = _stored(session, OLD)
    keeper = _stored(session, NEW, title="Keep")
    _synced(session, track_id, "s1")
    storage = _ListedStorage({NEW: b"x"})

    report = _reconcile(session, storage, syncs=[_sync_with()])

    assert (report.missing_new, report.missing_kept_in_library, report.left_library) == (1, 1, 0)
    sync_types = [sync_id_type("fakesync")]
    assert in_library(session, [track_id, keeper], sync_types) == {track_id, keeper}
    assert stored_file_paths(session, track_id) == []


def test_a_missing_file_that_comes_back_is_restored(session):
    track_id = _stored(session, NEW)
    keeper = _stored(session, "K/K - Keep.ogg", title="Keep")
    _reconcile(session, _ListedStorage({"K/K - Keep.ogg": b"x"}))
    assert track_id not in in_library(session, [track_id], [])

    report = _reconcile(session, _ListedStorage({"K/K - Keep.ogg": b"x", NEW: b"x"}))

    assert report.restored == 1
    assert _entry(session, track_id).missing_since is None
    assert in_library(session, [track_id, keeper], []) == {track_id, keeper}


def test_an_entry_still_missing_is_counted_once(session):
    _stored(session, OLD)
    _stored(session, NEW, title="Keep")
    storage = _ListedStorage({NEW: b"x"})
    _reconcile(session, storage)

    report = _reconcile(session, storage)

    assert (report.missing_new, report.missing_still) == (0, 1)


# ---------------------------------------------------------------------------
# No evidence, no change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("storage, status", [
    (_ListedStorage({NEW: b"x"}, listable=False), CANNOT_LIST),
    (_ListedStorage({"cover.jpg": b"x", ".work/part.ogg": b"x"}), EMPTY_LISTING),
    (_ListedStorage({"Someone/Else.ogg": b"x"}), LISTING_UNRELATED),
])
def test_without_a_trustworthy_listing_nothing_changes(session, storage, status):
    track_id = _stored(session, OLD)
    _stored(session, "B/B - Two.ogg", title="Two")

    report = _reconcile(session, storage)

    assert report.status == status
    assert _entry(session, track_id).missing_since is None and _entry(session, track_id).file_path == OLD


# ---------------------------------------------------------------------------
# Tracks with more than one file, for whoever decides which to keep
# ---------------------------------------------------------------------------


def test_tracks_left_with_more_than_one_file_are_listed_and_kept(session):
    track_id = _stored(session, NEW)
    second = "Artist/Album (2020)/Artist - Album - 01 - Song (1).ogg"
    session.add(LibraryEntry(track_id=track_id, file_path=second, file_format="ogg", storage_provider="shelf"))
    session.commit()
    storage = _ListedStorage({NEW: b"x", second: b"y"})

    report = _reconcile(session, storage)

    assert report.duplicates == [{"track_id": track_id, "artist": "Artist", "title": "Song",
                                  "paths": sorted([NEW, second])}]
    assert storage.deleted == [] and set(storage.files) == {NEW, second}


# ---------------------------------------------------------------------------
# The invariant: "in library" and full playback follow the files that exist
# ---------------------------------------------------------------------------


def test_in_library_and_full_playback_follow_the_files_that_exist(session):
    present = _stored(session, NEW, title="Present")
    relocated = _stored(session, "R/R (Unknown)/R - R - 00 - Moved.ogg", title="Moved", artist="R", album="R")
    lost = _stored(session, "L/L (Unknown)/L - L - 00 - Lost.ogg", title="Lost", artist="L", album="L")
    lost_synced = _stored(session, "S/S (Unknown)/S - S - 00 - Synced.ogg", title="Synced", artist="S", album="S")
    _synced(session, lost_synced, "s9")
    files = {NEW: b"x", "R/R (2020)/R - R - 01 - Moved.ogg": b"x"}
    storage = _ListedStorage(files)
    registry = FakeRegistry(storage=[storage], syncs=[_sync_with()])
    reconcile_library_files(session, registry, _config(), FakeAuthManager(), apply=True, now=NOW)

    ids = [present, relocated, lost, lost_synced]
    dicts = public_track_dicts(session, [session.get(TrackRecord, i) for i in ids])
    annotate_tracks(dicts, session, Viewer(user_id=None, stream_full=True), registry)
    rows = dict(zip(ids, dicts))

    for track_id in ids:
        stored = [path for path in stored_file_paths(session, track_id) if path in files]
        has_file = bool(stored) or track_id == lost_synced
        assert rows[track_id]["in_library"] is has_file, track_id
        assert (rows[track_id]["playback"]["type"] == "full") is has_file, track_id
        # Every stored path the library still names is a file the storage holds.
        assert stored == stored_file_paths(session, track_id)
    assert rows[lost]["download_status"] == ds.FAILED
