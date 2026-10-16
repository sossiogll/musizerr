"""
Automatic like matching (``services.favorites.match_likes``): the likes
imported from other providers that match a track already in the library
(``core.matching.match_track``, no bespoke comparison) become favorites,
without downloading anything. Near misses must not match. A like is matched
once: a favorite the user removes is not added back. Likes with no match
stay "liked elsewhere, not in library".
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import download_states as ds
from core.identifiers import sync_id_type
from core.routines import get_routine
from db.engine import get_session
from db.models import (
    SYSTEM_USER_ID,
    Download,
    LibraryEntry,
    TrackIdentifierRecord,
    TrackRecord,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import stored_track_data
from routes import deps
from routes.library import router as library_router
from services import favorites
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider, make_track
from tests.database import seed_parents, seed_system_user

USER = "u1"


def _record(session, title, artist="Band", album="Record", *, year=2020, duration_ms=200_000, isrc=None) -> str:
    track = make_track(title=title, artist=artist, album=album, year=year, duration_ms=duration_ms, isrc=isrc)
    record = TrackRecord(title=title, artist=artist, album=album, duration_ms=duration_ms,
                         track_data=stored_track_data(track))
    session.add(record)
    session.commit()
    if isrc:
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="isrc", value=isrc))
        session.commit()
    return record.internal_id


def _in_library(session, track_id, how="scan"):
    if how == "scan":
        session.add(LibraryEntry(track_id=track_id, file_path=f"{track_id}.flac", file_format="flac",
                                 source="scan", storage_provider="s"))
    elif how == "sync":
        session.add(TrackIdentifierRecord(track_id=track_id, id_type=sync_id_type("fakesync"), value=f"s-{track_id}"))
    else:
        session.add(Download(track_id=track_id, status=ds.DOWNLOADED))
    session.commit()


def _like(session, track_id, provider="elsewhere") -> str:
    seed_parents(session, users=[USER])
    saved = UserSavedTrack(user_id=USER, track_id=track_id)
    session.add(saved)
    session.flush()
    session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider=provider, provider_track_id=track_id))
    session.commit()
    return saved.id


REGISTRY = FakeRegistry(syncs=[FakeSyncProvider("fakesync")])


def _favorite_ids(session) -> set[str]:
    return {f.track_id for f in session.exec(select(UserFavorite).where(UserFavorite.user_id == USER)).all()}


def test_a_like_of_a_track_in_the_library_becomes_a_favorite_without_downloads(engine):
    with Session(engine) as session:
        same = _record(session, "Song")
        _in_library(session, same)
        _like(session, same)

        result = favorites.match_likes(session, REGISTRY)

        assert result == {"checked": 1, "matched": 1}
        assert _favorite_ids(session) == {same}
        assert session.exec(select(Download)).all() == []
        favorite = session.exec(select(UserFavorite)).one()
        assert favorite.source == "match"


@pytest.mark.parametrize("how", ["scan", "sync", "download"])
def test_a_like_matches_a_different_record_of_the_same_song(engine, how):
    """The library file was indexed from its tags as another record: the core
    matching (title + artists + album/year/duration) links them."""
    with Session(engine) as session:
        library = _record(session, "Song (Remastered)", duration_ms=201_000)
        _in_library(session, library, how)
        liked = _record(session, "Song", artist="band")
        saved_id = _like(session, liked)

        favorites.match_likes(session, REGISTRY)

        assert _favorite_ids(session) == {library}
        saved = session.get(UserSavedTrack, saved_id)
        assert saved.library_track_id == library and saved.matched_at is not None


def test_a_shared_isrc_matches_even_with_other_titles(engine):
    with Session(engine) as session:
        library = _record(session, "Canzone", isrc="IT123")
        _in_library(session, library)
        liked = _record(session, "Song", artist="Other", isrc="IT123")
        _like(session, liked)

        favorites.match_likes(session, REGISTRY)

        assert _favorite_ids(session) == {library}


@pytest.mark.parametrize("liked", [
    dict(title="Song Two"),                 # another title
    dict(title="Song", artist="Other Band"),  # another artist
    dict(title="Song", album="Other Record"),  # same title, another release
    dict(title="Song", year=1999),          # same title, another year
    dict(title="Son"),                      # a prefix is not the title
])
def test_near_misses_do_not_match(engine, liked):
    with Session(engine) as session:
        library = _record(session, "Song")
        _in_library(session, library)
        liked_id = _record(session, **{"title": "Song", **liked})
        saved_id = _like(session, liked_id)

        result = favorites.match_likes(session, REGISTRY)

        assert result["matched"] == 0
        assert _favorite_ids(session) == set()
        assert session.get(UserSavedTrack, saved_id).library_track_id is None


def test_a_like_is_matched_once_and_a_removed_favorite_stays_removed(engine):
    with Session(engine) as session:
        track = _record(session, "Song")
        _in_library(session, track)
        _like(session, track)
        favorites.match_likes(session, REGISTRY)
        favorites.remove(session, USER, track)

        assert favorites.match_likes(session, REGISTRY) == {"checked": 0, "matched": 0}
        assert _favorite_ids(session) == set()


def test_a_like_not_in_the_library_waits_and_matches_when_the_file_arrives(engine):
    with Session(engine) as session:
        track = _record(session, "Song")
        _like(session, track)

        assert favorites.match_likes(session, REGISTRY)["matched"] == 0
        _in_library(session, track, "download")
        assert favorites.match_likes(session, REGISTRY)["matched"] == 1
        assert _favorite_ids(session) == {track}


def test_liked_elsewhere_lists_only_the_likes_not_in_the_library(engine):
    with Session(engine) as session:
        in_lib = _record(session, "Here")
        _in_library(session, in_lib)
        _like(session, in_lib)
        away = _record(session, "Away")
        _like(session, away)
        favorites.match_likes(session, REGISTRY)

    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    app = FastAPI()
    app.include_router(library_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: REGISTRY
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    client = TestClient(app)
    # Open mode: the likes belong to the system user.
    seed_system_user(engine)
    with Session(engine) as session:
        for saved in session.exec(select(UserSavedTrack)).all():
            saved.user_id = SYSTEM_USER_ID
            session.add(saved)
        for favorite in session.exec(select(UserFavorite)).all():
            favorite.user_id = SYSTEM_USER_ID
            session.add(favorite)
        session.commit()

    elsewhere = client.get("/favorites/elsewhere").json()["items"]
    favorites_page = client.get("/favorites").json()["items"]

    assert [t["title"] for t in elsewhere] == ["Away"]
    assert elsewhere[0]["in_library"] is False and elsewhere[0]["favorite"] is False
    assert [t["title"] for t in favorites_page] == ["Here"]


def test_the_matching_is_a_routine_that_follows_the_library_index():
    """After a library sync or a scan the index routine starts, and the
    matching follows it (``tests/test_sync_index_routine.py`` runs the chain)."""
    import inspect

    from services import library_scan
    from workers import tasks

    routine = get_routine("match_likes")
    assert routine is not None and routine.task_name == "workers.tasks.match_likes"
    assert routine.follows == "sync_library_index"
    assert "start_routine(SYNC_LIBRARY_INDEX)" in inspect.getsource(tasks.sync_saved_tracks_for_user)
    assert "start_routine(SYNC_LIBRARY_INDEX)" in inspect.getsource(library_scan.scan_library_once)


def test_a_like_from_the_sync_provider_matches_by_its_id(engine):
    """The like imported from a provider that is also the sync provider
    carries as provider id the value the library record holds as sync id:
    the same item, whatever the metadata says."""
    with Session(engine) as session:
        library = _record(session, "Mil pasos (feat. Antoine Essertier)", artist="Soha", album="D'ici Et D'ailleurs",
                          year=2008, duration_ms=246_000)
        session.add(TrackIdentifierRecord(track_id=library, id_type=sync_id_type("fakesync"), value="3LeLAPhNgX47"))
        session.commit()
        liked = _record(session, "Mil pasos", artist="Soha", album="D'ici et d'ailleurs", year=2007, duration_ms=0)
        session.add(TrackIdentifierRecord(track_id=liked, id_type="fakesync_id", value="3LeLAPhNgX47"))
        session.commit()
        _like(session, liked, provider="fakesync")

        result = favorites.match_likes(session, REGISTRY)

        assert result["matched"] == 1
        assert _favorite_ids(session) == {library}


def test_a_like_matches_by_a_shared_provider_id(engine):
    with Session(engine) as session:
        library = _record(session, "Canzone", artist="Band")
        _in_library(session, library)
        session.add(TrackIdentifierRecord(track_id=library, id_type="elsewhere_id", value="e-1"))
        liked = _record(session, "Song", artist="Other")
        session.add(TrackIdentifierRecord(track_id=liked, id_type="elsewhere_id", value="e-1"))
        session.commit()
        _like(session, liked)

        assert favorites.match_likes(session, REGISTRY)["matched"] == 1
        assert _favorite_ids(session) == {library}


def test_a_one_year_gap_matches_when_album_and_duration_agree(engine):
    """"Mil pasos": the library file is tagged 2007, the like says 2008."""
    with Session(engine) as session:
        library = _record(session, "Mil pasos", artist="Soha", album="D'ici et d'ailleurs", year=2007,
                          duration_ms=246_000)
        _in_library(session, library)
        liked = _record(session, "Mil pasos (feat. Antoine Essertier)", artist="Soha", album="D'ici Et D'ailleurs",
                        year=2008, duration_ms=246_500)
        _like(session, liked)

        assert favorites.match_likes(session, REGISTRY)["matched"] == 1


def test_another_version_of_the_song_does_not_match(engine):
    with Session(engine) as session:
        library = _record(session, "E si fa bello per te - Remastered 2023", artist="Jovanotti",
                          album="E si fa bello per te (i remix)", year=2023, duration_ms=194_000)
        _in_library(session, library)
        liked = _record(session, "E si fa bello per te (Federico Scavo Remix)", artist="Jovanotti",
                        album="E si fa bello per te (i remix)", year=2023, duration_ms=194_000)
        _like(session, liked)

        assert favorites.match_likes(session, REGISTRY)["matched"] == 0
