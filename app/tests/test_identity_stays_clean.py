"""
The identity data stays clean while the app is used.

Seen live (2 Oct): the cleanup of 29 Sep brought the twin records to 0, and
three days of reading pages brought them back to 72. Two causes, both fixed:

- ``tracks.track_data`` carried a copy of the identifiers. The cleanup fixed
  ``track_identifiers`` and left the copy, and every list built from the
  copy brought the old sync id back;
- reading wrote: each GET indexed the "sync hits" it had just seen, so the
  coverage depended on what had been viewed.

Now identifiers live only in ``track_identifiers``, a read writes nothing,
and the presence on a sync provider is written by ``services.sync_index``.
"""
from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlmodel import Session, select

import main
from core import download_states as ds
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album, Artist, LibraryItem, Playlist
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.user_auth import create_access_token
from db.engine import get_session
from db.library_store import in_library
from db.models import Download, TrackIdentifierRecord, TrackRecord, User, UserFavorite, UserSavedTrack
from db.track_store import (
    find_or_create_track, identifiers_by_track, stored_track_data, track_data_dict, track_from_record,
)
from routes import deps
from services import sync_index
from services.capabilities import Viewer
from services.config_manager import ConfigManager
from services.stored_tracks import public_track_dict, public_track_dicts
from services.track_state import annotate_tracks

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track

SYNC = "fakesync"
SYNC_TYPE = sync_id_type(SYNC)
VIEWER = Viewer(user_id=None, stream_full=True)


def _snapshot(engine) -> dict:
    """Everything the identity data is made of, as plain data."""
    with Session(engine) as session:
        return {
            "tracks": sorted((r.internal_id, r.title, r.artist, r.album, r.track_data) for r in session.exec(select(TrackRecord)).all()),
            "identifiers": sorted((r.track_id, r.id_type, r.value) for r in session.exec(select(TrackIdentifierRecord)).all()),
            "favorites": sorted((r.user_id, r.track_id) for r in session.exec(select(UserFavorite)).all()),
            "saved": sorted((r.user_id, r.track_id, r.library_track_id) for r in session.exec(select(UserSavedTrack)).all()),
        }


def _record(session, title, *ids, artist="Soha", album="D'ici et d'ailleurs", legacy_ids=(), duration_ms=246_000,
            year=2007) -> str:
    track = make_track(title, artist, album, year=year, duration_ms=duration_ms)
    data = json.loads(stored_track_data(track))
    if legacy_ids:
        # A payload written before identifiers lived only in track_identifiers.
        data["external_ids"] = [dict(type=t, value=v) for t, v in legacy_ids]
    record = TrackRecord(title=title, artist=artist, album=album, duration_ms=duration_ms, track_data=json.dumps(data))
    session.add(record)
    session.commit()
    for id_type, value in ids:
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=id_type, value=value))
    session.commit()
    return record.internal_id


def _sync_values(engine, track_id) -> list[str]:
    with Session(engine) as session:
        return sorted(session.exec(select(TrackIdentifierRecord.value).where(
            TrackIdentifierRecord.track_id == track_id, TrackIdentifierRecord.id_type == SYNC_TYPE)).all())


# ---------------------------------------------------------------------------
# One source of truth: track_identifiers
# ---------------------------------------------------------------------------


def test_the_stored_payload_carries_no_identifiers():
    track = make_track("Song", "Artist", isrc="ISRC1", provider="catalog", provider_id="c1")
    track.add_id(Identifier(type=SYNC_TYPE, value="s1"))

    assert "external_ids" not in json.loads(stored_track_data(track))


def test_a_payload_written_before_is_read_without_its_identifiers():
    record = TrackRecord(title="Song", artist="Artist", album="Album", track_data=json.dumps({
        "title": "Song", "artist": "Artist", "album": "Album",
        "external_ids": [{"type": SYNC_TYPE, "value": "OLD"}, {"type": "isrc", "value": "ISRC1"}],
    }))

    assert "external_ids" not in track_data_dict(record)
    assert track_from_record(record).external_ids == []
    assert [i.storage_type() for i in track_from_record(record, [Identifier("isrc", "ISRC1")]).external_ids] == ["isrc"]


def test_a_list_row_carries_the_identifiers_of_the_table_not_the_payload(engine):
    with Session(engine) as session:
        current = _record(session, "Mil pasos", ("isrc", "FRZ110701478"), (SYNC_TYPE, "NEW"), legacy_ids=[(SYNC_TYPE, "OLD")])
        record = session.get(TrackRecord, current)

        row = public_track_dicts(session, [record])[0]

        assert {(e["type"], e["value"]) for e in row["external_ids"]} == {("isrc", "FRZ110701478"), (SYNC_TYPE, "NEW")}
        # A sync id is the id of a file, never "the id of a provider named <x>_sync".
        assert all("provider" not in e for e in row["external_ids"] if e["type"] == SYNC_TYPE)


def test_a_stale_sync_id_in_a_legacy_payload_is_not_brought_back_by_a_read(engine):
    """"Killing In The Name": the cleanup removed the stale sync id from
    ``track_identifiers``; the list built from the payload read it back."""
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        library = _record(session, "Mil pasos", ("isrc", "FRZ110701478"), (SYNC_TYPE, "NEW"), legacy_ids=[(SYNC_TYPE, "OLD")])
        row = public_track_dict(library, session.get(TrackRecord, library))

        annotate_tracks([row], session, VIEWER, registry)

        assert row["in_library"] is True
    assert _sync_values(engine, library) == ["NEW"]


def test_a_write_from_a_stored_record_cannot_bring_a_replaced_id_back(engine):
    registry = FakeRegistry(syncs=[FakeSyncProvider(SYNC)])
    with Session(engine) as session:
        library = _record(session, "Mil pasos", ("isrc", "FRZ110701478"), (SYNC_TYPE, "NEW"), legacy_ids=[(SYNC_TYPE, "OLD")])
        record = session.get(TrackRecord, library)
        # Whatever resolves the record again (an import, a download request)
        # from the canonical track: its identifiers are the table's.
        again = find_or_create_track(track_from_record(record, identifiers_by_track(session, [library])[library]), session)

        assert again.internal_id == library
        assert "OLD" not in json.loads(again.track_data).get("external_ids", [])
    assert _sync_values(engine, library) == ["NEW"]
    del registry


# ---------------------------------------------------------------------------
# Reading writes nothing
# ---------------------------------------------------------------------------


class _Catalog(MetadataProvider):
    name = "cat"
    label = "Cat"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    TITLES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]

    def __init__(self):
        super().__init__(StaticProviderContext())
        self.tracks = []
        for index, title in enumerate(self.TITLES):
            track = make_track(title, "Band", "Record", duration_ms=200_000 + index, provider=self.name,
                               provider_id=f"t{index}", isrc=f"ISRC{index}", year=2020)
            track.preview_url = f"https://cdn.example.com/p{index}.mp3"
            self.tracks.append(track)

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _album(self):
        album = Album(title="Record", artist="Band", year=2020, provider=self.name, provider_id="a1", album_type="album",
                      track_count=len(self.tracks), tracks=list(self.tracks))
        album.add_id(Identifier(type=PROVIDER_ID, value="a1", provider=self.name))
        return album

    def get_track(self, provider_id, session):
        return next((t for t in self.tracks if t.provider_id == provider_id), None)

    def get_album(self, provider_id, session):
        return self._album()

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider=self.name, provider_id="p1", tracks=list(self.tracks))

    def get_artist(self, provider_id, session):
        return Artist(name="Band", provider=self.name, provider_id="ar1")

    def get_artist_albums(self, provider_id, session, limit=50):
        return [self._album()]

    def get_artist_top_tracks(self, provider_id, session, limit=10):
        return list(self.tracks)[:limit]

    def search_tracks(self, query, session, limit=20):
        return list(self.tracks)

    def search_albums(self, query, session, limit=20):
        return [self._album()]

    def get_new_releases(self, session, limit=20):
        return [self._album()]

    def find_related_tracks(self, track, session, limit=20):
        return []


@pytest.fixture
def api(engine):
    """The whole API over ``engine``: a catalog, a sync provider that lists a
    library, a user. Everything is seeded the way the live data was (stale
    ids in the payloads, a file the database does not know)."""
    catalog = _Catalog()
    library = [
        LibraryItem(sync_id="S-ALPHA", path="a.flac", title="Alpha", artist="Band", album="Record", duration_ms=200_000, year=2020),
        LibraryItem(sync_id="S-BRAVO", path="b.flac", title="Bravo", artist="Band", album="Record", duration_ms=200_001, year=2020),
        LibraryItem(sync_id="S-DELTA-NEW", path="d.flac", title="Delta", artist="Band", album="Record", duration_ms=200_003, year=2020),
        LibraryItem(sync_id="S-ECHO", path="e.flac", title="Echo", artist="Band", album="Record", duration_ms=200_004, year=2020),
    ]
    sync = FakeSyncProvider(SYNC, {"alpha": "S-ALPHA", "bravo": "S-BRAVO", "delta": "S-DELTA-NEW", "echo": "S-ECHO"},
                            library=library)
    ids = {}
    with Session(engine) as session:
        user = User(username="u", password_hash="x", role="user")
        session.add(user)
        session.commit()
        user_id = user.id
        token = create_access_token(user.id, user.role)
        for index, title in enumerate(_Catalog.TITLES):
            track = catalog.tracks[index]
            legacy = [("isrc", f"ISRC{index}"), ("cat_id", f"t{index}")]
            table = [("isrc", f"ISRC{index}"), ("cat_id", f"t{index}")]
            if title == "Alpha":
                table.append((SYNC_TYPE, "S-ALPHA"))                    # indexed
            if title == "Delta":
                table.append((SYNC_TYPE, "S-DELTA-OLD"))                # renumbered since
                legacy.append((SYNC_TYPE, "S-DELTA-OLD"))
            if title == "Echo":
                legacy.append((SYNC_TYPE, "S-GONE"))                    # only in the payload
            ids[title] = _record(session, title, *table, artist="Band", album="Record", duration_ms=track.duration_ms,
                                 year=2020, legacy_ids=legacy)
        session.add(Download(track_id=ids["Charlie"], status=ds.DOWNLOADED, file_path="c.flac"))
        session.add(UserFavorite(user_id=user_id, track_id=ids["Alpha"]))
        session.add(UserSavedTrack(user_id=user_id, track_id=ids["Foxtrot"]))
        session.commit()

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True, "metadata_provider_priority": ["cat"], "sync_provider": SYNC})
    registry = FakeRegistry(metadata=[catalog], syncs=[sync])

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    from routes import discover
    with discover._cache_lock:
        discover._cache.clear()
    from services import enrichment
    enrichment._avail_cache.clear()
    client = TestClient(main.app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {token}"}
    yield {"get": lambda url: client.get(url, headers=headers), "ids": ids, "registry": registry, "sync": sync,
           "user_id": user_id}
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


PAGES = [
    "/album/cat/a1",
    "/playlist/cat/p1",
    "/artist/cat/ar1/albums",
    "/artist/cat/ar1/top-tracks",
    "/search?q=band&type=track",
    "/search?q=record&type=album",
    "/track/cat/t3",
    "/discover/new-releases",
    "/favorites",
    "/favorites/elsewhere",
]


def _coverage(body) -> list:
    """What every page says about the library, in one comparable list."""
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if "library_id" in node:
                found.append(("row", node.get("provider_id"), node["in_library"], node["download_status"], node["favorite"]))
            if "library" in node and isinstance(node["library"], dict):
                found.append(("coverage", node.get("title") or node.get("name"), json.dumps(node["library"], sort_keys=True)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found


def test_reading_every_page_writes_nothing(api, engine):
    before = _snapshot(engine)

    for url in PAGES:
        response = api["get"](url)
        assert response.status_code == 200, (url, response.text)

    assert _snapshot(engine) == before


def test_no_page_even_issues_a_write_statement(api, engine):
    """The snapshot cannot see a write that stores what was already there."""
    from sqlalchemy import event

    writes: list[str] = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement[:80])

    event.listen(engine, "before_cursor_execute", spy)
    try:
        for url in PAGES:
            assert api["get"](url).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    assert writes == []


def test_the_coverage_is_the_same_however_many_times_and_in_whatever_order_pages_are_read(api, engine):
    first = {}
    for url in PAGES:
        first[url] = _coverage(api["get"](url).json())
    before = _snapshot(engine)

    rng = random.Random(7)
    for _ in range(4):
        order = PAGES[:]
        rng.shuffle(order)
        for url in order:
            assert _coverage(api["get"](url).json()) == first[url], url

    assert _snapshot(engine) == before


def test_a_file_the_sync_provider_has_is_not_in_the_library_until_it_is_indexed(api, engine):
    """The row can play (the sync provider streams it) but the library is what
    the database holds: one answer for the row, the album and the discography."""
    before = _coverage(api["get"]("/artist/cat/ar1/albums").json())
    api["get"]("/album/cat/a1")
    api["get"]("/search?q=band&type=track")

    assert _coverage(api["get"]("/artist/cat/ar1/albums").json()) == before

    with Session(engine) as session:
        sync_index.index_libraries(session, api["registry"], FakeAuthManager())
    after = _coverage(api["get"]("/artist/cat/ar1/albums").json())
    assert after != before


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(order=st.lists(st.sampled_from(PAGES), min_size=1, max_size=12))
def test_any_sequence_of_reads_leaves_the_database_as_it_was(api, engine, order):
    before = _snapshot(engine)
    for url in order:
        api["get"](url)
    assert _snapshot(engine) == before


def test_a_like_that_names_one_of_two_files_does_not_drop_the_other(engine):
    with Session(engine) as session:
        record = _record(session, "Mil pasos", ("isrc", "I1"), (SYNC_TYPE, "FILE-A"), (SYNC_TYPE, "FILE-B"),
                         artist="Soha", album="D'ici et d'ailleurs")
        like = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", isrc="I1", provider=SYNC, provider_id="FILE-A",
                          year=2007, duration_ms=246_000)
        like.add_id(Identifier(type=SYNC_TYPE, value="FILE-A"))

        assert find_or_create_track(like, session).internal_id == record

    assert _sync_values(engine, record) == ["FILE-A", "FILE-B"]


# ---------------------------------------------------------------------------
# The indexing job
# ---------------------------------------------------------------------------


def test_the_index_gives_a_renumbered_file_its_new_id_and_drops_the_old_one(api, engine):
    delta = api["ids"]["Delta"]
    assert _sync_values(engine, delta) == ["S-DELTA-OLD"]

    with Session(engine) as session:
        reports = sync_index.index_libraries(session, api["registry"], FakeAuthManager())

    assert _sync_values(engine, delta) == ["S-DELTA-NEW"]
    assert reports[0]["stale_dropped"] == 1
    with Session(engine) as session:
        assert len(session.exec(select(TrackRecord)).all()) == len(_Catalog.TITLES)


def test_the_index_is_idempotent(api, engine):
    with Session(engine) as session:
        sync_index.index_libraries(session, api["registry"], FakeAuthManager())
    settled = _snapshot(engine)

    with Session(engine) as session:
        again = sync_index.index_libraries(session, api["registry"], FakeAuthManager())

    assert _snapshot(engine) == settled
    assert again[0]["created"] == again[0]["attached"] == again[0]["aliased"] == again[0]["stale_dropped"] == 0


def test_a_file_no_record_has_gets_one(api, engine):
    api["sync"]._library.append(LibraryItem(sync_id="S-NEW", path="n.flac", title="Zulu", artist="Other", album="Else",
                                            duration_ms=111_000))

    with Session(engine) as session:
        reports = sync_index.index_libraries(session, api["registry"], FakeAuthManager())
        record = session.exec(select(TrackRecord).where(TrackRecord.title == "Zulu")).one()
        assert in_library(session, [record.internal_id], [SYNC_TYPE]) == {record.internal_id}

    assert reports[0]["created"] == 1


def test_a_partial_listing_never_empties_the_library(api, engine):
    with Session(engine) as session:
        sync_index.index_libraries(session, api["registry"], FakeAuthManager())
    settled = _snapshot(engine)
    api["sync"]._library = api["sync"]._library[:1]

    with Session(engine) as session:
        reports = sync_index.index_libraries(session, api["registry"], FakeAuthManager())

    assert reports[0]["status"] == "partial_listing" and reports[0]["pruned"] is False
    assert _snapshot(engine) == settled


def test_an_empty_listing_changes_nothing(api, engine):
    api["sync"]._library = []
    before = _snapshot(engine)

    with Session(engine) as session:
        reports = sync_index.index_libraries(session, api["registry"], FakeAuthManager())

    assert reports[0]["status"] == "empty_listing"
    assert _snapshot(engine) == before


def test_a_record_with_two_live_files_keeps_both(api, engine):
    """A duplicate in the library: both files are live, neither replaces the other."""
    bravo = api["ids"]["Bravo"]
    api["sync"]._library.append(LibraryItem(sync_id="S-BRAVO-2", path="b2.flac", title="Bravo", artist="Band",
                                            album="Record", duration_ms=200_001, year=2020))

    for _ in range(2):
        with Session(engine) as session:
            sync_index.index_libraries(session, api["registry"], FakeAuthManager())

    assert _sync_values(engine, bravo) == ["S-BRAVO", "S-BRAVO-2"]


def test_a_track_the_sync_provider_has_is_indexed_when_asked_about_it(api, engine):
    charlie = api["ids"]["Charlie"]
    api["sync"]._available["charlie"] = "S-CHARLIE"

    with Session(engine) as session:
        assert sync_index.index_track(session, api["registry"], FakeAuthManager(), charlie) is True
        assert sync_index.index_track(session, api["registry"], FakeAuthManager(), api["ids"]["Foxtrot"]) is False

    assert _sync_values(engine, charlie) == ["S-CHARLIE"]


def test_rounds_of_reads_after_an_index_change_nothing_more(api, engine):
    """After the job the data is settled: no twin, no drift, the same coverage
    on every read."""
    with Session(engine) as session:
        sync_index.index_libraries(session, api["registry"], FakeAuthManager())
    settled = _snapshot(engine)
    first = {url: _coverage(api["get"](url).json()) for url in PAGES}

    for _ in range(3):
        for url in PAGES:
            assert _coverage(api["get"](url).json()) == first[url], url

    assert _snapshot(engine) == settled
    with Session(engine) as session:
        per_value: dict[str, set[str]] = {}
        for row in session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.id_type == SYNC_TYPE)).all():
            per_value.setdefault(row.value, set()).add(row.track_id)
        assert all(len(owners) == 1 for owners in per_value.values()), "a sync id names two records"


# ---------------------------------------------------------------------------
# Where the jobs run
# ---------------------------------------------------------------------------


@pytest.fixture
def worker(api, engine, monkeypatch):
    from workers import tasks

    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: api["registry"])
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks, "shared_store", lambda session: None)
    return tasks


def test_the_index_routine_indexes_the_sync_library(api, engine, worker):
    """The routine ``sync_library_index`` (periodically, and after a library
    scan or a likes sync): the presence on the sync provider is written there."""
    delta = api["ids"]["Delta"]

    result = worker.sync_library_index_task.run()

    assert result["status"] == "ok"
    assert [r["sync_provider"] for r in result["providers"]] == [SYNC]
    assert _sync_values(engine, delta) == ["S-DELTA-NEW"]


def test_the_like_matching_job_does_not_index(api, engine, worker):
    """The matching follows the index routine; it never indexes itself."""
    before = _snapshot(engine)["identifiers"]

    result = worker.match_likes_task.run()

    assert "checked" in result and result["library_index"] == {"status": "never"}
    assert _snapshot(engine)["identifiers"] == before


def test_the_index_routine_fails_when_the_sync_provider_is_down(api, engine, worker):
    def down(session):
        raise RuntimeError("[Errno -3] Temporary failure in name resolution")

    api["sync"].list_library = down
    before = _snapshot(engine)

    result = worker.sync_library_index_task.run()

    assert result["status"] == "error" and result["error_code"] == "sync_provider_unreachable"
    assert _snapshot(engine)["identifiers"] == before["identifiers"]


def test_the_liked_elsewhere_total_is_what_the_page_lists(api, engine):
    """Seen live: "declared total 1363, returned 1037": the page dropped rows
    after counting them, because reading indexed them as in the library."""
    body = api["get"]("/favorites/elsewhere").json()

    assert body["total"] == len(body["items"]) == 1
    assert api["get"]("/favorites/elsewhere").json()["total"] == body["total"]


def test_after_the_cleanup_rounds_of_reads_keep_the_twins_at_zero_and_the_coverage_as_it_is(api, engine):
    """The regression of 2 Oct: the cleanup brought the twins to 0 and the
    reads of the next days brought them back (72). Here the data is dirty (a
    twin, a renumbered file, a stale id only a payload had), the cleanup runs
    on it, and then typical reads of every kind of page are repeated."""
    from scripts import track_identity_cleanup as cleanup

    live = {item.sync_id: item for item in api["sync"]._library}
    with Session(engine) as session:
        _record(session, "Alpha", (f"{SYNC}_id", "S-ALPHA"), artist="Band", album="Record", duration_ms=200_000, year=2020)
        before = cleanup.metrics(session, {SYNC})
        cleanup.refresh_stale_sync_ids(session, SYNC, live)
        cleanup.merge_twins(session, SYNC)
        cleanup.add_sync_aliases(session, SYNC, live)
        after = cleanup.metrics(session, {SYNC})
    assert before["twin_values_provider_id_vs_sync_id"] == 1
    assert after["twin_values_provider_id_vs_sync_id"] == 0
    settled = _snapshot(engine)
    first = {url: _coverage(api["get"](url).json()) for url in PAGES}

    rng = random.Random(2)
    for _ in range(5):
        order = PAGES[:]
        rng.shuffle(order)
        for url in order:
            api["get"](url)
            assert _coverage(api["get"](url).json()) == first[url], url

    assert _snapshot(engine) == settled
    with Session(engine) as session:
        assert cleanup.metrics(session, {SYNC}) == after


def test_the_download_worker_does_not_fetch_a_second_copy_of_what_the_sync_provider_has(engine, monkeypatch):
    """The track is on the sync provider although no job has indexed it yet: the
    worker records it and lands the request instead of downloading again."""
    from core.download_resolver import ResolveResult
    from services import progress
    from workers import tasks

    sync = FakeSyncProvider(SYNC, {"mil pasos": "S-MIL"})
    config = ConfigManager(engine=engine)
    config.update({"max_retries": 0})
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(progress, "engine", engine)
    monkeypatch.setattr(tasks, "get_config", lambda: config)
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry(syncs=[sync]))
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (None, "disk"))
    monkeypatch.setattr(tasks.DownloadResolver, "resolve", lambda *a, **k: pytest.fail("a second copy was fetched"))
    with Session(engine) as session:
        track_id = _record(session, "Mil pasos")
        download = Download(track_id=track_id, status=ds.PENDING)
        session.add(download)
        session.commit()
        download_id = download.id

    track = make_track("Mil pasos", "Soha")
    track.internal_id = track_id
    result = tasks.download_task.run(track_data=track.to_dict(), download_id=download_id)

    assert result["detail"] == "already_in_library"
    with Session(engine) as session:
        assert session.get(Download, download_id).status == ds.DOWNLOADED
    assert _sync_values(engine, track_id) == ["S-MIL"]
