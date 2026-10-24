"""
Copertura della libreria calcolata dal server: ``library: {total,
downloaded, in_progress, failed}`` per album e playlist, per ogni uscita
della discografia di un artista e come riepilogo dell'artista. I conteggi
seguono le righe di download seminate, in ogni stato.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core import download_states as ds
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.identifiers import PROVIDER_ID, Identifier
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Album, Artist, Playlist
from musizeer_sdk.context import StaticProviderContext
from db.engine import get_session
from db.models import Download, TrackIdentifierRecord, TrackRecord
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, make_track

STATES = [ds.DOWNLOADED, ds.DOWNLOADED, ds.DOWNLOADING, ds.PENDING, ds.FAILED, None]


def test_coverage_counts_each_state():
    assert ds.coverage(STATES) == {"total": 6, "downloaded": 2, "in_progress": 2, "failed": 1}


def test_coverage_uses_the_known_total_when_larger():
    assert ds.coverage([ds.DOWNLOADED], total=10)["total"] == 10
    assert ds.coverage([], total=None) == {"total": 0, "downloaded": 0, "in_progress": 0, "failed": 0}


class _Catalog(MetadataProvider):
    name = "cov"
    label = "Cov"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self):
        super().__init__(StaticProviderContext())
        self.tracks = [
            make_track(title=f"Song {i}", artist="Caparezza", album="Prisoner 709", provider=self.name, provider_id=f"t{i}")
            for i in range(len(STATES))
        ]

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_album(self, provider_id, session):
        album = Album(title="Prisoner 709", artist="Caparezza", year=2017, provider=self.name,
                      provider_id="a1", track_count=16, tracks=list(self.tracks))
        album.add_id(Identifier(type=PROVIDER_ID, value="a1", provider=self.name))
        return album

    def get_playlist(self, provider_id, session):
        return Playlist(name="List", provider=self.name, provider_id="p1", tracks=list(self.tracks))

    def get_artist(self, provider_id, session):
        return Artist(name="Caparezza", provider=self.name, provider_id="ar1")

    def get_artist_albums(self, provider_id, session, limit=50):
        deluxe = Album(title="Prisoner 709 (Deluxe)", artist="Caparezza", year=2017, provider=self.name,
                       provider_id="a2", track_count=18)
        other = Album(title="Museica", artist="Caparezza", year=2014, provider=self.name, provider_id="a3", track_count=19)
        return [self.get_album("a1", session), deluxe, other]


@pytest.fixture
def world():
    engine = new_engine()
    catalog = _Catalog()
    with Session(engine) as session:
        for track, state in zip(catalog.tracks, STATES):
            record = TrackRecord(title=track.title, artist=track.artist, album=track.album)
            session.add(record)
            session.commit()
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=f"{catalog.name}_id", value=track.provider_id))
            if state is not None:
                session.add(Download(track_id=record.internal_id, status=state))
            session.commit()

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False, "metadata_provider_priority": [catalog.name]})
    app = FastAPI()
    for router in (album_router, playlist_router, artist_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[catalog])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as client:
        yield client


EXPECTED_SEEN = {"downloaded": 2, "in_progress": 2, "failed": 1}


def test_album_coverage(world):
    library = world.get("/album/cov/a1").json()["library"]

    assert library == {"total": 16, **EXPECTED_SEEN}


def test_playlist_coverage(world):
    library = world.get("/playlist/cov/p1").json()["library"]

    assert library == {"total": len(STATES), **EXPECTED_SEEN}


def test_discography_coverage_per_release_and_for_the_artist(world):
    data = world.get("/artist/cov/ar1/albums").json()
    by_title = {item["title"]: item["library"] for item in data["items"]}

    # La deluxe è un'edizione di Prisoner 709: le tracce scaricate contano lì.
    assert by_title["Prisoner 709"] == {"total": 18, **EXPECTED_SEEN}
    assert by_title["Museica"] == {"total": 19, "downloaded": 0, "in_progress": 0, "failed": 0}
    assert data["library"] == {"total": 37, **EXPECTED_SEEN}


# ---------------------------------------------------------------------------
# services.library_coverage direttamente
# ---------------------------------------------------------------------------


def _seed(engine, title, album, status, *, year=2017, artist="Caparezza"):
    from core.serialization import serialize_track
    import json

    track = make_track(title=title, artist=artist, album=album, year=year)
    with Session(engine) as session:
        record = TrackRecord(title=title, artist=artist, album=album, track_data=json.dumps(serialize_track(track)))
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, status=status))
        session.commit()


def _groups(*albums):
    from core.discography import group_releases

    return group_releases(list(albums), ["cov"])


def test_the_same_song_in_two_editions_counts_once_with_its_best_state(engine):
    from services.library_coverage import release_coverage

    _seed(engine, "Larsen", "Prisoner 709", ds.FAILED)
    _seed(engine, "Larsen", "Prisoner 709 (Deluxe)", ds.DOWNLOADED)
    groups = _groups(Album(title="Prisoner 709", artist="Caparezza", year=2017, provider="cov"),
                     Album(title="Prisoner 709 (Deluxe)", artist="Caparezza", year=2017, provider="cov"))

    with Session(engine) as session:
        [coverage] = release_coverage(groups, session, FakeRegistry())

    # No edition says how many tracks the release has: the total is unknown
    # ("1 in the library"), never the one song seen ("1/1").
    assert coverage == {"total": None, "downloaded": 1, "in_progress": 0, "failed": 0}


def test_the_year_keeps_releases_with_the_same_title_apart(engine):
    from services.library_coverage import release_coverage

    _seed(engine, "Buddy Holly", "Weezer", ds.DOWNLOADED, year=1994, artist="Weezer")
    groups = _groups(Album(title="Weezer", artist="Weezer", year=1994, provider="cov", provider_id="blue"),
                     Album(title="Weezer", artist="Weezer", year=2001, provider="cov", provider_id="green"))

    with Session(engine) as session:
        coverages = release_coverage(groups, session, FakeRegistry())

    by_release = {g.representative.provider_id: c["downloaded"] for g, c in zip(groups, coverages)}
    assert by_release == {"blue": 1, "green": 0}


def test_only_tracks_of_the_releases_shown_are_read_in_full(engine, monkeypatch):
    from services import library_coverage

    for i in range(20):
        _seed(engine, f"Other {i}", f"Unrelated {i}", ds.DOWNLOADED, artist="Someone Else")
    _seed(engine, "Larsen", "Prisoner 709", ds.DOWNLOADED)
    read = []
    real = library_coverage.track_from_record
    monkeypatch.setattr(library_coverage, "track_from_record", lambda record: read.append(record.title) or real(record))

    with Session(engine) as session:
        library_coverage.release_coverage(_groups(Album(title="Prisoner 709", artist="Caparezza", year=2017, provider="cov")), session, FakeRegistry())

    assert read == ["Larsen"]


def test_two_versions_of_one_song_are_two_songs(engine):
    from services.library_coverage import release_coverage

    _seed(engine, "E si fa bello per te - Remastered 2023", "E si fa bello per te (i remix)", ds.DOWNLOADED,
          year=2023, artist="Jovanotti")
    _seed(engine, "E si fa bello per te (Federico Scavo Remix)", "E si fa bello per te (i remix)", ds.FAILED,
          year=2023, artist="Jovanotti")
    release = Album(title="E si fa bello per te (i remix)", artist="Jovanotti", year=2023, provider="cov", track_count=10)

    with Session(engine) as session:
        [coverage] = release_coverage(_groups(release), session, FakeRegistry())

    assert coverage == {"total": 10, "downloaded": 1, "in_progress": 0, "failed": 1}


def test_the_track_count_of_a_sync_provider_is_never_the_total(engine):
    from core.discography import ReleaseGroup
    from services.library_coverage import release_coverage
    from tests.conftest import FakeSyncProvider

    _seed(engine, "E si fa bello per te - Remastered 2023", "E si fa bello per te (i remix)", ds.DOWNLOADED,
          year=2023, artist="Jovanotti")
    # The sync provider lists the album with the one file the library has.
    synced = Album(title="E si fa bello per te (i remix)", artist="Jovanotti", year=2023, provider="fakesync",
                   track_count=1)

    with Session(engine) as session:
        [coverage] = release_coverage([ReleaseGroup(representative=synced, editions=[synced])], session,
                                      FakeRegistry(syncs=[FakeSyncProvider("fakesync")]))

    assert coverage["total"] is None
    assert coverage["downloaded"] == 1


def test_the_artist_summary_total_is_unknown_when_a_release_total_is():
    from services.library_coverage import summarize

    known = {"total": 10, "downloaded": 1, "in_progress": 0, "failed": 0}
    unknown = {"total": None, "downloaded": 2, "in_progress": 0, "failed": 0}

    assert summarize([known, unknown]) == {"total": None, "downloaded": 3, "in_progress": 0, "failed": 0}
    assert summarize([known, known])["total"] == 20
