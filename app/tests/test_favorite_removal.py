"""
A favorite the user took off stays off, through the whole cycle.

Before, it stayed removed only because the imported like kept its
``matched_at``: it came back when a re-import resolved the like to another
record, and after "Elimina dati libreria" followed by a new sync. Now the
removal is remembered by what names the song (``core.matching.song_keys``:
its stable identifiers and its title identity with each artist), per user,
and honoured by every automatic path. Only the user's own heart or download
clears it.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlmodel import Session, select

from core import download_states as ds
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from core.matching import song_keys
from db import favorite_store
from db.models import (
    Download, PendingLike, RemovedFavorite, TrackIdentifierRecord, TrackRecord, User, UserFavorite, UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import stored_track_data
from routes.library import delete_saved_tracks
from services import favorites
from workers import tasks

from tests.conftest import FakeRegistry, FakeSyncProvider, make_track

REGISTRY = FakeRegistry(syncs=[FakeSyncProvider("fakesync")])


class _Config:
    sync_provider = ""


@pytest.fixture(autouse=True)
def _no_sync_provider_no_tasks(monkeypatch):
    dispatched: list[tuple] = []
    monkeypatch.setattr(favorites, "dispatch", lambda name, **kwargs: dispatched.append((name, kwargs)))
    monkeypatch.setattr(favorites, "get_config", lambda: _Config())
    return dispatched


def _user(session, name="alice") -> str:
    user = User(username=name, password_hash="x", role="user")
    session.add(user)
    session.commit()
    return user.id


def _library_track(session, title="Killing In The Name", artist="Rage Against The Machine", *, isrc="USSM19200022",
                   album="Rage Against The Machine", duration_ms=313_000, catalog_id_value="sp-kitn") -> str:
    """A track in the library: a download that landed."""
    track = make_track(title, artist, album, duration_ms=duration_ms, provider="catalog", provider_id=catalog_id_value, isrc=isrc,
                       year=1992)
    record = TrackRecord(title=title, artist=artist, album=album, duration_ms=duration_ms, track_data=stored_track_data(track))
    session.add(record)
    session.commit()
    for identifier in track.external_ids:
        session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type=identifier.storage_type(), value=identifier.value))
    session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path=f"{record.internal_id}.flac"))
    session.commit()
    return record.internal_id


def _like_track(title="Killing In The Name", artist="Rage Against The Machine", *, provider_id="sp-kitn", isrc="USSM19200022",
                album="Rage Against The Machine"):
    track = make_track(title, artist, album, duration_ms=313_000, provider="catalog", provider_id=provider_id, isrc=isrc,
                       year=1992)
    track.add_id(Identifier(type=PROVIDER_ID, value=provider_id, provider="catalog"))
    return track


def _import(session, user_id, track) -> None:
    """What the likes sync does with the likes it fetched."""
    tasks._persist_saved_tracks([track], user_id, "catalog", session, REGISTRY)


def _favorite(session, user_id) -> list[UserFavorite]:
    return session.exec(select(UserFavorite).where(UserFavorite.user_id == user_id)).all()


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


def test_import_match_remove_reimport_stays_removed(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        _import(session, user, _like_track())
        assert favorites.match_likes(session, REGISTRY)["matched"] == 1
        assert [f.track_id for f in _favorite(session, user)] == [library]

        assert favorites.remove(session, user, library) is True
        assert _favorite(session, user) == []

        _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)

        assert _favorite(session, user) == []


def test_it_stays_removed_when_the_like_resolves_to_another_record(engine):
    """A re-import after an identity merge or split: the like is on a record
    that is not the one the favorite was on, and the library holds the song
    under yet another."""
    with Session(engine) as session:
        user = _user(session)
        first = _library_track(session)
        _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)
        favorites.remove(session, user, first)

        # The song is now another record of the library (other ISRC, other
        # release), and the like was imported again onto a record of its own.
        session.exec(Download.__table__.delete())
        second = _library_track(session, isrc="USSM19200999", catalog_id_value="sp-other")
        session.exec(UserSavedTrackProvider.__table__.delete())
        session.exec(UserSavedTrack.__table__.delete())
        session.commit()
        _import(session, user, _like_track(provider_id="sp-reissue", isrc=None, album=""))
        like = session.exec(select(UserSavedTrack)).one()
        assert like.track_id == first, "the like came back on the old record"

        result = favorites.match_likes(session, REGISTRY)

        assert result["matched"] == 1
        like = session.exec(select(UserSavedTrack)).one()
        assert like.library_track_id == second, "linked to the library, on the other record"
        assert _favorite(session, user) == []


def test_it_stays_removed_after_the_imported_likes_are_wiped_and_synced_again(engine):
    with Session(engine) as session:
        user_id = _user(session)
        library = _library_track(session)
        _import(session, user_id, _like_track())
        favorites.match_likes(session, REGISTRY)
        favorites.remove(session, user_id, library)

        delete_saved_tracks(session, session.get(User, user_id))
        assert session.exec(select(UserSavedTrack)).all() == []
        _import(session, user_id, _like_track())
        favorites.match_likes(session, REGISTRY)

        assert _favorite(session, user_id) == []


def test_a_like_that_is_still_on_the_origin_provider_does_not_bring_it_back(engine):
    """Removing a favorite only takes the like back on the sync provider: the
    other provider still lists it, and every sync imports it again."""
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)
        favorites.remove(session, user, library)

        for _ in range(3):
            _import(session, user, _like_track())
            favorites.match_likes(session, REGISTRY)

        assert _favorite(session, user) == []


def test_removing_one_users_favorite_does_not_touch_another(engine):
    with Session(engine) as session:
        alice, bob = _user(session, "alice"), _user(session, "bob")
        library = _library_track(session)
        for user in (alice, bob):
            _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)

        favorites.remove(session, alice, library)
        for user in (alice, bob):
            _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)

        assert _favorite(session, alice) == []
        assert [f.track_id for f in _favorite(session, bob)] == [library]


# ---------------------------------------------------------------------------
# Only the user's own action brings it back
# ---------------------------------------------------------------------------


def test_a_new_heart_clears_the_removal(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)
        favorites.remove(session, user, library)
        assert favorites.is_removed(session, user, library)

        favorites.add(session, user, library, REGISTRY)

        assert [f.source for f in _favorite(session, user)] == [favorites.HEART]
        assert not favorites.is_removed(session, user, library)
        assert session.exec(select(RemovedFavorite)).all() == []


def test_a_download_of_theirs_clears_the_removal(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        favorites.add(session, user, library, REGISTRY)
        favorites.remove(session, user, library)

        favorites.register_intent(session, user, library, REGISTRY)

        assert [f.source for f in _favorite(session, user)] == [favorites.DOWNLOAD]
        assert not favorites.is_removed(session, user, library)


def test_a_download_still_on_its_way_clears_it_when_asked_not_when_it_lands(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        favorites.add(session, user, library, REGISTRY)
        favorites.remove(session, user, library)
        session.exec(Download.__table__.delete())
        session.commit()

        favorites.register_intent(session, user, library, REGISTRY)

        assert session.exec(select(PendingLike)).one().sync_provider is None
        assert not favorites.is_removed(session, user, library)


def test_after_a_new_heart_and_a_new_removal_it_stays_off_again(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)
        _import(session, user, _like_track())
        favorites.match_likes(session, REGISTRY)
        for _ in range(2):
            favorites.remove(session, user, library)
            _import(session, user, _like_track())
            favorites.match_likes(session, REGISTRY)
            assert _favorite(session, user) == []
            favorites.add(session, user, library, REGISTRY)
            assert len(_favorite(session, user)) == 1


def test_removing_something_that_is_not_a_favorite_remembers_nothing(engine):
    with Session(engine) as session:
        user = _user(session)
        library = _library_track(session)

        assert favorites.remove(session, user, library) is False

        assert session.exec(select(RemovedFavorite)).all() == []


# ---------------------------------------------------------------------------
# What names a song
# ---------------------------------------------------------------------------


def test_the_keys_of_a_song_are_the_same_on_every_record_and_source():
    a = make_track("Killing In The Name", "Rage Against The Machine", provider="catalog", provider_id="x", isrc="I1")
    b = make_track("Killing In The Name - Remastered 2009", "Rage Against the Machine, Zack", provider="catalog2", provider_id="y")

    assert song_keys(a) & song_keys(b) == {"song:killing in the name|rage against the machine"}


def test_a_remix_and_a_sync_id_are_not_names_of_the_song():
    original = make_track("Song", "Band")
    remix = make_track("Song - Federico Scavo Remix", "Band")
    original.add_id(Identifier(type="fakesync_sync_id", value="file-1"))

    assert not song_keys(original) & song_keys(remix)
    assert not any(key.startswith("fakesync_sync_id") for key in song_keys(original))


def test_a_removal_is_cleared_with_all_its_keys(engine):
    with Session(engine) as session:
        user = _user(session)
        favorite_store.record_removal(session, user, ["isrc:A", "song:x|b"])
        favorite_store.record_removal(session, user, ["song:x|b", "isrc:B"])

        # The user favorites a record named by isrc:B and the same song title.
        assert favorite_store.clear_removal(session, user, ["isrc:B", "song:x|b"]) >= 1

        assert not favorite_store.is_removed(session, user, ["isrc:A", "isrc:B", "song:x|b"])


# ---------------------------------------------------------------------------
# Model-based: any order of actions
# ---------------------------------------------------------------------------

ACTIONS = ["import", "match", "remove", "heart", "wipe", "new_record"]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(st.sampled_from(ACTIONS), min_size=1, max_size=14))
def test_whatever_the_order_a_removed_favorite_is_back_only_by_the_users_own_heart(engine, steps):
    """The user's last word on the song decides: after a removal, nothing
    automatic (imports, matching, wipes, a new record of the song) makes it a
    favorite; a heart does."""
    with Session(engine) as session:
        user = _user(session, "model")
        library = [_library_track(session)]
        removed = False
        for step in steps:
            if step == "import":
                _import(session, user, _like_track(provider_id=f"sp-{len(library)}"))
            elif step == "match":
                favorites.match_likes(session, REGISTRY)
            elif step == "remove":
                if favorites.remove(session, user, library[-1]):
                    removed = True
            elif step == "heart":
                favorites.add(session, user, library[-1], REGISTRY)
                removed = False
            elif step == "wipe":
                delete_saved_tracks(session, session.get(User, user))
            elif step == "new_record":
                session.exec(Download.__table__.delete())
                library.append(_library_track(session, isrc=f"USSM1920{len(library):04d}", album=f"Edition {len(library)}",
                                              catalog_id_value=f"sp-lib-{len(library)}"))
                # The song is on a new record now: whatever was a favorite on the old
                # one is the user's business, not an automatic one.
                session.exec(UserFavorite.__table__.delete())
                session.commit()
                removed = removed or favorites.is_removed(session, user, library[-1])
            if removed:
                assert not any(f.source == favorites.MATCH for f in _favorite(session, user)), steps
        # Back to a clean database for the next example.
        for table in (RemovedFavorite, UserFavorite, UserSavedTrackProvider, UserSavedTrack, PendingLike, Download,
                      TrackIdentifierRecord, TrackRecord, User):
            session.exec(table.__table__.delete())
        session.commit()
