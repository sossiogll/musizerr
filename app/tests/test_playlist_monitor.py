"""
Followed playlists, watched (routine ``refresh_followed_playlists``,
``services/playlist_monitor.py``): the provider's snapshot decides whether the
tracks are read again, a change is applied and its new tracks downloaded
through the approval policy, a failing source is recorded without stopping the
others and makes the run an error, a playlist that cannot be looked at is
skipped. Fake providers only.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.auth_manager import AuthError
from core.download_policy import KIND_PLAYLIST, MODE_APPROVAL, mode_key
from db import playlist_store
from db.models import DownloadRequest, Playlist, PlaylistTrack, TrackRecord, User
from services import downloads, playlist_monitor, playlists
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, make_track
from tests.playlists_support import Source


@pytest.fixture
def host(engine, monkeypatch, sent_tasks):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})  # nobody governed: the gate queues at once
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    monkeypatch.setattr(playlist_monitor, "auth_manager_for", lambda store: FakeAuthManager())
    source = Source()
    registry = FakeRegistry(metadata=[source])
    with Session(engine) as session:
        owner = User(username="alice", password_hash="x")
        session.add(owner)
        session.commit()
        owner_id = owner.id

    class Host:
        pass

    host = Host()
    host.engine, host.config, host.source, host.registry = engine, config, source, registry
    host.queued, host.owner_id, host.sent = queued, owner_id, sent_tasks
    return host


def _follow(host, provider_id="p1") -> str:
    with Session(host.engine) as session:
        owner = session.get(User, host.owner_id)
        playlist = host.source.get_playlist(provider_id, None)
        result = playlists.follow(session, owner, playlist, config=host.config, registry=host.registry,
                                  auth_manager=FakeAuthManager())
        return result.playlist.id


def _refresh(host) -> dict:
    with Session(host.engine) as session:
        return playlist_monitor.refresh_all(session, host.registry, host.config)


def _titles(host, playlist_id) -> list[str]:
    with Session(host.engine) as session:
        rows = playlist_store.track_rows(session, playlist_id)
        return [session.get(TrackRecord, r.track_id).title for r in rows]


def test_an_unchanged_snapshot_costs_one_cheap_call_and_reads_no_track(host):
    _follow(host)
    reads_before = host.source.playlist_calls
    host.queued.clear()

    result = _refresh(host)

    assert host.source.snapshot_calls == 1
    assert host.source.playlist_calls == reads_before
    assert (result["status"], result["playlists"], result["checked"], result["changed"], result["new_tracks"]) == (
        "ok", 1, 1, 0, 0)
    assert host.queued == []


def test_a_new_snapshot_applies_the_change_and_downloads_only_the_new_tracks(host):
    playlist_id = _follow(host)
    host.queued.clear()
    host.sent.clear()
    host.source.tracks = [host.source.tracks[0], host.source.tracks[2],
                          make_track(title="Song 4", provider="catalog", provider_id="t4")]
    host.source.snapshot = "v2"

    result = _refresh(host)

    assert (result["status"], result["changed"], result["new_tracks"], result["removed_tracks"],
            result["download_requests"]) == ("ok", 1, 1, 1, 1)
    assert _titles(host, playlist_id) == ["Song 1", "Song 3", "Song 4"]
    assert [item["track_data"]["title"] for item in host.queued] == ["Song 4"]
    # The change is mirrored on the sync providers soon.
    assert [(name, kwargs) for name, kwargs, _ in host.sent] == [
        ("workers.routines.start", {"routine_key": "sync_playlists"})]
    with Session(host.engine) as session:
        assert playlist_store.get(session, playlist_id).snapshot_id == "v2"


def test_the_new_tracks_wait_for_approval_when_the_policy_says_so(host, monkeypatch):
    _follow(host)
    host.queued.clear()
    host.config.update({"auth_enabled": True, mode_key(KIND_PLAYLIST): MODE_APPROVAL})
    host.source.tracks = host.source.tracks + [make_track(title="Song 4", provider="catalog", provider_id="t4")]
    host.source.snapshot = "v2"

    result = _refresh(host)

    assert (result["new_tracks"], result["download_requests"]) == (1, 1)
    assert host.queued == []
    with Session(host.engine) as session:
        assert [(r.kind, r.status, r.needed_tracks) for r in session.exec(select(DownloadRequest))] == [
            ("playlist", "pending", 1)]


def test_a_followed_playlist_that_does_not_download_still_follows_the_source(host):
    playlist_id = _follow(host)
    with Session(host.engine) as session:
        playlists.update(session, session.get(Playlist, playlist_id), auto_download=False)
    host.queued.clear()
    host.source.tracks = host.source.tracks[:1]
    host.source.snapshot = "v2"

    result = _refresh(host)

    assert (result["changed"], result["removed_tracks"], result["download_requests"]) == (1, 2, 0)
    assert _titles(host, playlist_id) == ["Song 1"]
    assert host.queued == []


def test_a_failing_source_is_recorded_and_fails_the_run_without_stopping_the_others(host):
    first = _follow(host, "p1")
    second = _follow(host, "p2")
    host.source.fail_ids = {"p1"}
    host.source.snapshot = "v2"
    host.source.tracks = host.source.tracks[:2]

    result = _refresh(host)

    assert (result["status"], result["error_code"]) == ("error", "metadata_provider_unreachable")
    assert [f["playlist_id"] for f in result["failed"]] == [first]
    assert (result["checked"], result["changed"]) == (2, 1)
    assert _titles(host, second) == ["Song 1", "Song 2"]
    with Session(host.engine) as session:
        assert playlist_store.get(session, first).refresh_error_code == "metadata_provider_unreachable"
        assert playlist_store.get(session, second).refresh_error_code is None

    # The next run reads it again, and a good answer clears the failure.
    host.source.fail_ids = set()
    ok = _refresh(host)
    assert ok["status"] == "ok"
    with Session(host.engine) as session:
        assert playlist_store.get(session, first).refresh_error_code is None


def test_a_source_that_is_gone_fails_the_run_with_its_code(host):
    playlist_id = _follow(host)
    host.source.exists = False
    host.source.snapshot = "v2"

    result = _refresh(host)

    assert (result["status"], result["error_code"]) == ("error", "playlist_source_not_found")
    assert _titles(host, playlist_id) == ["Song 1", "Song 2", "Song 3"]


def test_a_playlist_that_cannot_be_looked_at_is_skipped_not_failed(host, monkeypatch):
    _follow(host)
    with Session(host.engine) as session:
        other = User(username="bob", password_hash="x")
        session.add(other)
        session.commit()
        bob_id = other.id
        playlists.follow(session, other, host.source.get_playlist("p9", None), config=host.config,
                         registry=host.registry, auth_manager=FakeAuthManager())
        stranger = Playlist(user_id=bob_id, name="Elsewhere", kind="followed", source_provider="gone", source_id="x")
        session.add(stranger)
        session.commit()

    class _NoAccount(FakeAuthManager):
        def authenticate(self, provider):
            raise AuthError("not connected")

    managers = iter([FakeAuthManager(), _NoAccount()])
    monkeypatch.setattr(playlist_monitor, "auth_manager_for", lambda store: next(managers))

    result = _refresh(host)

    # alice's is read; bob's has no account on its provider; the third names a provider not installed.
    assert (result["status"], result["playlists"], result["checked"], result["skipped"]) == ("ok", 3, 1, 2)


def test_an_inactive_owner_is_not_looked_after(host):
    _follow(host)
    with Session(host.engine) as session:
        owner = session.get(User, host.owner_id)
        owner.is_active = False
        session.add(owner)
        session.commit()
    host.source.snapshot = "v2"

    result = _refresh(host)

    assert (result["checked"], result["skipped"]) == (0, 1)
    assert host.source.snapshot_calls == 0


def test_progress_is_reported_for_every_playlist(host):
    _follow(host, "p1")
    _follow(host, "p2")
    seen = []

    with Session(host.engine) as session:
        playlist_monitor.refresh_all(
            session, host.registry, host.config,
            progress=lambda phase, **kw: seen.append((phase, kw["provider"], kw["processed"], kw["total"])))

    assert seen == [("refreshing", "catalog", 0, 2), ("refreshing", "catalog", 1, 2)]


def test_nothing_followed_is_an_ok_run_that_starts_nothing(host):
    result = _refresh(host)

    assert result["status"] == "ok" and (result["playlists"], result["checked"]) == (0, 0)
    assert host.sent == []
    with Session(host.engine) as session:
        assert session.exec(select(PlaylistTrack)).all() == []
