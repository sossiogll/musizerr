"""
Versions of one song never collapse into one library track, and the album
page and the discography tile agree (analysis of 29 Sep).

The case, with provider-neutral data shaped like what two catalogs and a
library server really answered for one release:

- two catalogs ("alpha" and "beta") list the same ten-track release, "the
  remixes of a hit": one remaster and nine remixes with the same base title,
  each with its own ISRC. They write the version differently (``Title
  (Alpha Remix)`` against ``Title - Alpha Remix``) and give their own ids;
- the library server ("shelf", a sync provider that is also a catalog) holds
  ONE of them (the remaster) and lists the release with ``track_count: 1``.

Before the fix every remix resolved to the library record of the remaster
(library 10/10, 10 favorites) and the discography tile said 1/1. How a real
provider turns its service's answer into these tracks is tested in its own
repository.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import SyncCandidate, SyncProvider
from musizerr_sdk.models import Album, AlbumRef, Artist, ArtistRef, Track
from core.identifiers import sync_id_type
from db.engine import get_session
from db.models import TrackIdentifierRecord, TrackRecord
from db.track_store import stored_track_data
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from services.config_manager import ConfigManager
from services.enrichment import cross_enrich_album, enrich_tracks_with_stream_url

from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry
from tests.database import new_engine

ARTIST = "Some Artist"
RELEASE = "Hit Song (the remixes)"
BASE_TITLE = "Hit Song"
# (version, length in ms)
VERSIONS = [
    ("Remastered 2023", 194_040), ("Alpha Remix", 254_995), ("Beta Remix", 182_460), ("Gamma Remix", 173_333),
    ("Delta Remix", 195_839), ("Epsilon Remix", 219_000), ("Zeta Remix", 269_508), ("Eta Remix", 160_124),
    ("Theta Remix", 306_000), ("Iota Remix", 211_000),
]
ISRCS = [f"ZZABC230{n:04d}" for n in range(1, 11)]
REMASTER_ISRC = ISRCS[0]
ALPHA_ARTIST, ALPHA_RELEASE = "3315", "418574377"
BETA_ARTIST, BETA_RELEASE = "beta-artist-1", "beta-release-1"
SHELF_ARTIST, SHELF_RELEASE = "shelf-artist-1", "shelf-release-1"
REMASTER_BETA_ID = "beta-track-1"
REMASTER_SYNC_ID = "shelf-track-1"

NONE = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)


def _track(provider: str, n: int, *, style: str, artist_id: str, release_id: str) -> Track:
    version, length = VERSIONS[n]
    title = f"{BASE_TITLE} ({version})" if style == "paren" else f"{BASE_TITLE} - {version}"
    track = Track(
        title=title, artist=ARTIST, album=RELEASE, duration_ms=length, provider=provider, provider_id=f"{provider}-track-{n + 1}",
        artist_provider_id=artist_id, album_provider_id=release_id, year=2023, track_number=n + 1, disc_number=1,
        albums=[AlbumRef(RELEASE, provider, release_id)], artists=[ArtistRef(ARTIST, provider, artist_id)],
        providers=[provider],
    )
    track.add_id(Identifier(type=ISRC, value=ISRCS[n]))
    track.add_id(Identifier(type=PROVIDER_ID, value=track.provider_id, provider=provider))
    return track


def _release(provider: str, tracks: list[Track], *, artist_id: str, release_id: str, track_count: int | None = None) -> Album:
    return Album(
        title=RELEASE, artist=ARTIST, year=2023, track_count=track_count if track_count is not None else len(tracks),
        provider=provider, provider_id=release_id, artist_provider_id=artist_id, album_type="album",
        tracks=tracks, providers=[provider],
    )


class _Catalog(FakeMetadataProvider):
    """A catalog that knows one artist, one release and its tracks: it
    answers a search with the release WITHOUT its tracks and a lookup with
    them."""

    auth = NONE

    def __init__(self, name: str, tracks: list[Track], *, artist_id: str, release_id: str) -> None:
        super().__init__(name)
        self.artist_id, self.release_id = artist_id, release_id
        self.tracks = tracks
        self.artist = Artist(name=ARTIST, provider=name, provider_id=artist_id)

    def full(self) -> Album:
        return _release(self.name, self.tracks, artist_id=self.artist_id, release_id=self.release_id)

    def _stub(self) -> Album:
        return _release(self.name, [], artist_id=self.artist_id, release_id=self.release_id, track_count=len(self.tracks))

    def search_albums(self, query, session, limit=20):
        return [self._stub()]

    def search_artists(self, query, session, limit=20):
        return [self.artist]

    def get_album(self, provider_id, session):
        return self.full() if provider_id == self.release_id else None

    def get_artist(self, provider_id, session):
        return self.artist if provider_id == self.artist_id else None

    def get_artist_albums(self, provider_id, session, limit=50):
        return [self._stub()] if provider_id == self.artist_id else []

    def get_track(self, provider_id, session):
        return next((t for t in self.tracks if t.provider_id == provider_id), None)


class _Shelf(_Catalog, SyncProvider):
    """The library server: a catalog of the files it holds (ONE track) and a
    sync provider that finds them by title."""

    def __init__(self) -> None:
        song = Track(
            title=f"{BASE_TITLE} ({VERSIONS[0][0]})", artist=ARTIST, album=RELEASE, duration_ms=VERSIONS[0][1], provider="shelf",
            provider_id=REMASTER_SYNC_ID, artist_provider_id=SHELF_ARTIST, album_provider_id=SHELF_RELEASE, year=2023,
            track_number=1, disc_number=1, providers=["shelf"],
        )
        song.add_id(Identifier(type=ISRC, value=REMASTER_ISRC))
        song.add_id(Identifier(type=PROVIDER_ID, value=REMASTER_SYNC_ID, provider="shelf"))
        super().__init__("shelf", [song], artist_id=SHELF_ARTIST, release_id=SHELF_RELEASE)

    def trigger_scan(self, session) -> None:
        return None

    def find_candidates(self, track, session):
        # A server search by title: everything of the library that has that title.
        if not track.title.lower().startswith(BASE_TITLE.lower()):
            return []
        return [SyncCandidate(sync_id=song.provider_id, track=song) for song in self.tracks]

    def get_stream_url(self, sync_id, session):
        return f"http://shelf.test/stream/{sync_id}"


@pytest.fixture
def providers():
    alpha = _Catalog("alpha", [_track("alpha", n, style="paren", artist_id=ALPHA_ARTIST, release_id=ALPHA_RELEASE) for n in range(10)],
                     artist_id=ALPHA_ARTIST, release_id=ALPHA_RELEASE)
    beta = _Catalog("beta", [_track("beta", n, style="dash", artist_id=BETA_ARTIST, release_id=BETA_RELEASE) for n in range(10)],
                    artist_id=BETA_ARTIST, release_id=BETA_RELEASE)
    return alpha, beta, _Shelf()


@pytest.fixture
def registry(providers):
    alpha, beta, shelf = providers
    # The sync provider answers before the other catalog: the order that
    # made the discography copy its track count (1) onto the release.
    return FakeRegistry(metadata=[alpha, shelf, beta], syncs=[shelf])


# ---------------------------------------------------------------------------
# Cross-enrichment
# ---------------------------------------------------------------------------


def test_each_remix_takes_the_id_of_its_own_version_on_the_other_catalog(providers, registry):
    album = providers[0].full()

    cross_enrich_album(album, registry, FakeAuthManager())

    expected = [t.provider_id for t in providers[1].tracks]
    got = [next((i.value for i in t.external_ids if i.storage_type() == "beta_id"), None) for t in album.tracks]
    assert got == expected


def test_a_sync_album_with_fewer_tracks_enriches_no_track(providers, registry):
    album = providers[0].full()

    cross_enrich_album(album, registry, FakeAuthManager())

    carrying = [t.title for t in album.tracks if any(i.storage_type().startswith("shelf") for i in t.external_ids)]
    assert carrying == []
    assert album.track_count == 10


def test_only_the_version_in_the_library_is_available_on_the_sync_provider(providers, registry):
    album = providers[0].full()

    enrich_tracks_with_stream_url(album.tracks, registry, FakeAuthManager())

    sync_type = sync_id_type("shelf")
    hits = [t.title for t in album.tracks if any(i.storage_type() == sync_type for i in t.external_ids)]
    assert hits == [f"{BASE_TITLE} ({VERSIONS[0][0]})"]


# ---------------------------------------------------------------------------
# Album page and discography tile, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def client(providers, registry):
    engine = new_engine()
    _, beta, _ = providers
    remaster = beta.tracks[0]
    with Session(engine) as session:
        # The library record of the one file on disk (the remaster), as the
        # like sync and the stream enrichment left it.
        record = TrackRecord(title=remaster.title, artist=remaster.artist, album=remaster.album,
                             duration_ms=remaster.duration_ms, track_data=stored_track_data(remaster))
        session.add(record)
        session.commit()
        for id_type, value in (("isrc", REMASTER_ISRC), ("beta_id", REMASTER_BETA_ID),
                               (sync_id_type("shelf"), REMASTER_SYNC_ID)):
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=id_type, value=value))
        session.commit()

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": ["alpha", "beta", "shelf"]})
    app = FastAPI()
    app.include_router(album_router)
    app.include_router(artist_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as test_client:
        yield test_client


def test_the_album_page_counts_one_version_in_the_library(client):
    data = client.get(f"/album/alpha/{ALPHA_RELEASE}").json()

    in_library = [t["title"] for t in data["tracks"] if t["in_library"]]
    assert in_library == [f"{BASE_TITLE} ({VERSIONS[0][0]})"]
    assert data["library"] == {"total": 10, "downloaded": 1, "in_progress": 0, "failed": 0}
    library_ids = [t["library_id"] for t in data["tracks"] if t["library_id"]]
    assert len(library_ids) == len(set(library_ids))


def test_the_discography_tile_agrees_with_the_album_page(client):
    album_page = client.get(f"/album/alpha/{ALPHA_RELEASE}").json()["library"]
    discography = client.get(f"/artist/alpha/{ALPHA_ARTIST}/albums").json()

    tiles = [item for item in discography["items"] if item["title"] == RELEASE]
    assert len(tiles) == 1
    tile = tiles[0]["library"]
    assert (tile["downloaded"], tile["total"]) == (album_page["downloaded"], album_page["total"]) == (1, 10)


def test_the_release_keeps_the_artist_id_of_its_own_provider(client):
    discography = client.get(f"/artist/alpha/{ALPHA_ARTIST}/albums").json()

    [tile] = [item for item in discography["items"] if item["title"] == RELEASE]
    assert tile["provider"] == "alpha"
    assert tile["artist_provider_id"] == ALPHA_ARTIST
