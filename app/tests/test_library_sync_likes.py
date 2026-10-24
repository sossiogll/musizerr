"""
The likes of a provider that has no ISRC, through the host: the library sync
imports them (reading every page, once), the like matching turns the ones that
name a track of the library into favorites (title, artist and duration, with a
different version never taken for the original), and a provider whose service
refuses for now (an exhausted quota) fails the sync instead of looking like
"no likes".

The provider here is a generic fake: its likes are pages of tracks as it
would map them from its service. How a real one cleans a title is tested in
its own repository.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizeer_sdk.errors import ProviderRateLimited
from musizeer_sdk.models import Track
from db import library_job_store as store
from db.models import (
    LibraryEntry,
    LibraryJobRun,
    TrackRecord,
    User,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import stored_track_data
from services import favorites, library_jobs
from workers import tasks

from tests.conftest import FakeMetadataProvider, FakeRegistry, FakeSyncProvider, make_track

PROVIDER = "likesource"


class _Likes(FakeMetadataProvider):
    """A metadata provider with a user library and no catalog: its likes come
    in ``pages``; ``refuse`` makes its service refuse for now."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, pages: list[list[Track]], *, refuse: bool = False) -> None:
        super().__init__(PROVIDER)
        self.pages = pages
        self.refuse = refuse
        self.pages_read = 0

    def supports_user_library(self) -> bool:
        return True

    def iter_saved_tracks(self, session, page_size=50):
        if self.refuse:
            raise ProviderRateLimited("quota exhausted")
        for page in self.pages:
            self.pages_read += 1
            yield page


class _Auth:
    def authenticate(self, provider):
        return AuthSession.anonymous()


def _like(vid: str, title: str, artist: str, duration_ms: int) -> Track:
    return make_track(title=title, artist=artist, album="", duration_ms=duration_ms, provider=PROVIDER, provider_id=vid)


@pytest.fixture
def world(engine, monkeypatch):
    """A user, a registry with the provider, the worker wired to them."""

    def build(provider: _Likes) -> dict:
        monkeypatch.setattr(library_jobs, "publish_event", lambda event: None)
        registry = FakeRegistry(metadata=[provider], syncs=[FakeSyncProvider("fakesync")])
        monkeypatch.setattr(tasks, "engine", engine)
        monkeypatch.setattr(tasks, "get_registry", lambda: registry)
        monkeypatch.setattr(tasks, "own_store", lambda session, user_id: None)
        monkeypatch.setattr(tasks, "has_own_account", lambda store, provider: True)
        monkeypatch.setattr(tasks, "auth_manager_for", lambda s: _Auth())
        monkeypatch.setattr(tasks.routines, "start_routine", lambda name: None)
        with Session(engine) as session:
            user = User(username="alice", password_hash="x", role="user")
            session.add(user)
            session.commit()
            user_id = user.id
        return {"engine": engine, "registry": registry, "user_id": user_id, "provider": provider}

    return build


def _sync(world) -> LibraryJobRun:
    engine = world["engine"]
    with Session(engine) as session:
        run = store.create_run(session, world["user_id"], "library_sync")
    tasks.sync_saved_tracks_for_user.run(user_id=world["user_id"], run_id=run.id)
    with Session(engine) as session:
        return session.get(LibraryJobRun, run.id)


def _library_track(session, title, artist, *, duration_ms=215_000, album="Record") -> str:
    track = make_track(title=title, artist=artist, album=album, duration_ms=duration_ms)
    record = TrackRecord(title=title, artist=artist, album=album, duration_ms=duration_ms,
                         track_data=stored_track_data(track))
    session.add(record)
    session.commit()
    session.add(LibraryEntry(track_id=record.internal_id, file_path=f"{record.internal_id}.flac",
                             file_format="flac", source="scan", storage_provider="s"))
    session.commit()
    return record.internal_id


def test_likes_become_favorites_through_the_like_matching(world):
    provider = _Likes([
        [_like("a1", "Song", "Band", 215_000), _like("a2", "Other Song", "Band", 241_000)],
        [],  # a page with nothing to keep is not the end of the library
        [_like("b1", "Not In The Library", "Someone", 300_000)],
    ])
    w = world(provider)
    with Session(w["engine"]) as session:
        song = _library_track(session, "Song", "Band", duration_ms=215_000)
        other = _library_track(session, "Other Song", "Band", duration_ms=241_000)
        _library_track(session, "Unrelated", "Band", duration_ms=215_000)

    run = _sync(w)

    assert run.status == "completed"
    assert json.loads(run.result) == {"tracks_synced": 3}
    assert provider.pages_read == 3  # every page, once
    with Session(w["engine"]) as session:
        assert len(session.exec(select(UserSavedTrack)).all()) == 3
        providers = session.exec(select(UserSavedTrackProvider)).all()
        assert {p.provider for p in providers} == {PROVIDER}
        assert {p.provider_track_id for p in providers} == {"a1", "a2", "b1"}

        result = favorites.match_likes(session, w["registry"])

        assert result == {"checked": 3, "matched": 2}
        assert {f.track_id for f in session.exec(select(UserFavorite)).all()} == {song, other}


def test_a_version_longer_than_the_track_is_not_taken_for_it(world):
    """A video with an intro and an outro is not the audio track: the duration
    beyond the tolerance rejects, as for any other source."""
    w = world(_Likes([[_like("a1", "Song", "Band", 285_000)]]))
    with Session(w["engine"]) as session:
        _library_track(session, "Song", "Band", duration_ms=215_000)

    _sync(w)

    with Session(w["engine"]) as session:
        assert favorites.match_likes(session, w["registry"])["matched"] == 0
        assert session.exec(select(UserFavorite)).all() == []
        assert len(session.exec(select(UserSavedTrack)).all()) == 1  # still imported, "liked elsewhere"


def test_a_remix_like_does_not_match_the_original_in_the_library(world):
    w = world(_Likes([[_like("a1", "Song (Some Producer Remix)", "Band", 215_000)]]))
    with Session(w["engine"]) as session:
        _library_track(session, "Song", "Band", duration_ms=215_000)

    _sync(w)

    with Session(w["engine"]) as session:
        assert favorites.match_likes(session, w["registry"])["matched"] == 0


def test_a_service_that_refuses_for_now_fails_the_sync_with_its_code_and_imports_nothing(world):
    w = world(_Likes([[_like("a1", "Song", "Band", 215_000)]], refuse=True))

    run = _sync(w)

    assert run.status == "failed"
    assert run.error_code == "metadata_provider_rate_limited"
    entries = {e["provider"]: e for e in json.loads(run.providers)}
    assert entries[PROVIDER]["status"] == "failed"
    assert entries[PROVIDER]["error_code"] == "metadata_provider_rate_limited"
    with Session(w["engine"]) as session:
        assert session.exec(select(UserSavedTrack)).all() == []
