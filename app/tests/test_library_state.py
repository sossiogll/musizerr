"""
One server definition of "in library" (``db.library_store``), the same on the
track row and in every coverage: a download that landed, a file found by the
library scan, a track indexed by the sync provider (``services.sync_index``).
A provider that streams full tracks does NOT put a track in the library —
the bug this fixes: the row said "Downloaded" (derived from ``stream_url``)
while the coverage said "0 of 1 in library". Neither does a sync provider
that answered during the request: reading writes nothing, so the library
does not depend on what was viewed.

Playback, decided on the server too: full from the library, full from a
provider that declares ``streams_full_tracks`` (or a sync provider), preview,
none.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core import download_states as ds
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import MetadataProvider
from musizerr_sdk.models import Album, Artist, LibraryItem, Playlist
from musizerr_sdk.context import StaticProviderContext
from core.user_auth import create_access_token
from db.engine import get_session
from db.library_store import file_states, in_library
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord, User
from db.track_store import stored_track_data
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.discover import router as discover_router
from services import sync_index
from services.capabilities import Viewer
from services.config_manager import ConfigManager
from services.track_state import annotate_tracks

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track

# How each track of the album is (or is not) in the library.
SOURCES = ["download", "scan", "sync_stored", "sync_live", "failed", "nothing", "provider_stream"]
IN_LIBRARY = {"download", "scan", "sync_stored"}


class _Catalog(MetadataProvider):
    name = "cat"
    label = "Cat"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, *, streams_full: bool = False):
        super().__init__(StaticProviderContext())
        self.streams_full_tracks = streams_full
        self.tracks = []
        for i, source in enumerate(SOURCES):
            track = make_track(title=f"Song {i}", artist="Band", album="Record", provider=self.name,
                               provider_id=f"t{i}", year=2020)
            track.preview_url = f"https://cdn.example.com/p{i}.mp3"
            if source == "provider_stream":
                track.stream_url = f"https://cdn.example.com/full{i}.mp3"
            self.tracks.append(track)

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _album(self):
        album = Album(title="Record", artist="Band", year=2020, provider=self.name, provider_id="a1",
                      track_count=len(SOURCES), tracks=[t for t in self.tracks])
        album.add_id(Identifier(type=PROVIDER_ID, value="a1", provider=self.name))
        return album

    def get_album(self, provider_id, session):
        return self._album()

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider=self.name, provider_id="p1", tracks=list(self.tracks))

    def get_artist(self, provider_id, session):
        return Artist(name="Band", provider=self.name, provider_id="ar1")

    def get_artist_albums(self, provider_id, session, limit=50):
        return [self._album()]

    def get_new_releases(self, session, limit=20):
        return [self._album()]


def _seed(engine, catalog: _Catalog) -> dict[str, str]:
    """A record for every track but the one only found live on the sync provider
    (not indexed yet)."""
    ids = {}
    with Session(engine) as session:
        for track, source in zip(catalog.tracks, SOURCES):
            if source == "sync_live":
                continue
            record = TrackRecord(title=track.title, artist=track.artist, album=track.album,
                                 track_data=stored_track_data(track))
            session.add(record)
            session.commit()
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="cat_id", value=track.provider_id))
            if source == "download":
                session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path="x.flac"))
            elif source == "scan":
                session.add(LibraryEntry(track_id=record.internal_id, file_path="scan.flac", file_format="flac",
                                         source="scan", storage_provider="s"))
            elif source == "sync_stored":
                session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=sync_id_type("fakesync"),
                                                  value="sync-stored"))
            elif source == "failed":
                session.add(Download(track_id=record.internal_id, status=ds.FAILED))
            session.commit()
            ids[source] = record.internal_id
    return ids


def _client(engine, *, streams_full=False, auth_enabled=False):
    catalog = _Catalog(streams_full=streams_full)
    ids = _seed(engine, catalog)
    sync = FakeSyncProvider("fakesync", {"song 3": "sync-live"})
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": auth_enabled, "metadata_provider_priority": ["cat"]})
    app = FastAPI()
    for router in (album_router, playlist_router, artist_router, discover_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[catalog], syncs=[sync])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    from routes import discover
    with discover._cache_lock:
        discover._cache.clear()
    return TestClient(app), ids


def _by_source(tracks: list[dict]) -> dict[str, dict]:
    return {SOURCES[int(t["provider_id"][1:])]: t for t in tracks}


def test_each_source_puts_a_track_in_the_library_and_only_those(engine):
    client, _ids = _client(engine)

    rows = _by_source(client.get("/album/cat/a1").json()["tracks"])

    for source, row in rows.items():
        assert row["in_library"] is (source in IN_LIBRARY), source
        assert (row["download_status"] == ds.DOWNLOADED) is (source in IN_LIBRARY), source
    assert rows["failed"]["download_status"] == ds.FAILED
    assert rows["nothing"]["download_status"] is None
    assert rows["provider_stream"]["download_status"] is None


def test_the_row_and_the_coverage_agree_everywhere(engine):
    client, _ids = _client(engine, streams_full=True)

    album = client.get("/album/cat/a1").json()
    playlist = client.get("/playlist/cat/p1").json()
    rows_in_library = sum(t["in_library"] for t in album["tracks"])

    assert rows_in_library == len(IN_LIBRARY)
    assert album["library"]["downloaded"] == rows_in_library
    assert playlist["library"]["downloaded"] == sum(t["in_library"] for t in playlist["tracks"])
    # The discography and the Home cards count from the same database.
    discography = client.get("/artist/cat/ar1/albums").json()
    assert discography["items"][0]["library"]["downloaded"] == rows_in_library
    assert discography["library"]["downloaded"] == rows_in_library
    home = client.get("/discover/new-releases").json()["items"][0]
    assert home["library"]["downloaded"] == rows_in_library


def test_a_provider_full_stream_is_not_the_library(engine):
    """The reported case: a provider that streams full tracks made the row
    say "Downloaded" while the album said "0 of 1 in library"."""
    client, _ids = _client(engine, streams_full=True)

    album = client.get("/album/cat/a1").json()
    row = _by_source(album["tracks"])["provider_stream"]

    assert row["in_library"] is False and row["download_status"] is None
    assert row["playback"] == {"type": "full", "source": "provider"}
    assert row["stream_url"] == "https://cdn.example.com/full6.mp3"


def test_a_live_sync_hit_plays_but_is_not_in_the_library_until_the_job_indexes_it(engine):
    client, _ids = _client(engine)
    row = _by_source(client.get("/album/cat/a1").json()["tracks"])["sync_live"]

    assert row["in_library"] is False and row["library_id"] is None
    assert row["playback"] == {"type": "full", "source": "provider"}
    with Session(engine) as session:
        assert session.exec(TrackIdentifierRecord.__table__.select().where(TrackIdentifierRecord.value == "sync-live")).first() is None

    sync = FakeSyncProvider("fakesync", library=[LibraryItem(sync_id="sync-live", path="p", title="Song 3", artist="Band",
                                                              album="Record", duration_ms=200_000, year=2020)])
    with Session(engine) as session:
        sync_index.index_libraries(session, FakeRegistry(syncs=[sync]), FakeAuthManager())
        record = session.exec(
            TrackIdentifierRecord.__table__.select().where(TrackIdentifierRecord.value == "sync-live")
        ).first()
        assert record is not None
        assert in_library(session, [record.track_id], [sync_id_type("fakesync")]) == {record.track_id}


def test_file_states_prefers_the_library_over_a_failed_attempt(engine):
    with Session(engine) as session:
        record = TrackRecord(title="T", artist="A", album="B")
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, status=ds.FAILED))
        session.add(LibraryEntry(track_id=record.internal_id, file_path="t.flac", file_format="flac",
                                 source="scan", storage_provider="s"))
        session.commit()

        assert file_states(session, [record.internal_id], []) == {record.internal_id: ds.DOWNLOADED}


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streams_full", [False, True])
def test_playback_for_a_signed_in_viewer(engine, streams_full):
    client, ids = _client(engine, streams_full=streams_full)

    rows = _by_source(client.get("/album/cat/a1").json()["tracks"])

    for source in IN_LIBRARY:
        assert rows[source]["playback"] == {"type": "full", "source": "library"}, source
        assert rows[source]["stream_url"] == f"/api/stream/{rows[source]['library_id']}"
    for source in ("failed", "nothing"):
        assert rows[source]["playback"] == {"type": "preview", "source": "provider"}
        assert rows[source]["stream_url"] is None
    # Found live on the sync provider, not indexed: it streams the whole track.
    assert rows["sync_live"]["playback"] == {"type": "full", "source": "provider"}
    provider_row = rows["provider_stream"]
    if streams_full:
        assert provider_row["playback"] == {"type": "full", "source": "provider"}
    else:
        # A stream_url from a provider that does not declare full streaming is not trusted.
        assert provider_row["playback"] == {"type": "preview", "source": "provider"}
        assert provider_row["stream_url"] is None


def test_a_guest_gets_previews_only(engine):
    client, _ids = _client(engine, streams_full=True, auth_enabled=True)

    rows = client.get("/album/cat/a1").json()["tracks"]

    assert {r["playback"]["type"] for r in rows} == {"preview"}
    assert all(r["stream_url"] is None for r in rows)
    assert all(r["favorite"] is False and r["favorite_pending"] is False for r in rows)


def test_no_preview_and_no_file_means_nothing_to_play(engine):
    with Session(engine) as session:
        track = {"kind": "track", "internal_id": "x", "title": "Silent", "artist": "A", "album": "B",
                 "duration_ms": 1, "provider": "cat", "provider_id": "zz", "preview_url": None,
                 "stream_url": None, "external_ids": []}
        annotate_tracks([track], session, Viewer(user_id=None, stream_full=True), FakeRegistry())

    assert track["playback"] == {"type": "none", "source": None}


def test_the_signed_in_viewer_sees_their_own_favorites(engine):
    from db.models import UserFavorite

    client, ids = _client(engine, auth_enabled=True)
    with Session(engine) as session:
        user = User(username="u", password_hash="x", role="user")
        session.add(user)
        session.commit()
        session.add(UserFavorite(user_id=user.id, track_id=ids["download"]))
        session.commit()
        token = create_access_token(user.id, user.role)

    rows = _by_source(client.get("/album/cat/a1", headers={"Authorization": f"Bearer {token}"}).json()["tracks"])
    album = client.get("/album/cat/a1", headers={"Authorization": f"Bearer {token}"}).json()

    assert rows["download"]["favorite"] is True
    assert all(not rows[s]["favorite"] for s in SOURCES if s != "download")
    assert album["favorites"] == {"favorite": 1, "addable": len(IN_LIBRARY) - 1}
