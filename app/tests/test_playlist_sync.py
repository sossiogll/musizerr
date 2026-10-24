"""
The playlists of the users, mirrored on the sync providers (routine
``sync_playlists``, ``services/playlist_sync.py``): each user's own account,
only the tracks the provider holds, nothing pushed twice, the copies of deleted
playlists deleted, a copy the user deleted on the server created again, one
failure never stopping the rest. Fake providers only.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.auth import AccountTarget
from core.auth_manager import AuthManager
from core.identifiers import sync_id_type
from musizeer_sdk.identifiers import Identifier
from db import playlist_store
from db.auth_store import CredentialStore
from db.models import Playlist, PlaylistSync, PlaylistTrack, TrackRecord, User
from db.track_store import add_identifiers
from services import playlist_sync

from tests.conftest import FakeRegistry, FakeSyncProvider
from tests.playlists_support import PlaylistSyncProvider

SYNC = "fakesync"


@pytest.fixture(autouse=True)
def _real_credentials(monkeypatch):
    """The accounts are the real thing (the store, the routing rule); only the
    expiry policy, which reads the host's configuration, is fixed."""
    monkeypatch.setattr(playlist_sync, "auth_manager_for", lambda store: AuthManager(store, token_expiry_buffer=0))


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def sync():
    return PlaylistSyncProvider(SYNC)


def _user(session, name: str, *, account: bool = True) -> str:
    user = User(username=name, password_hash="x")
    session.add(user)
    session.commit()
    if account:
        CredentialStore(session, user_id=user.id).save_credentials(
            SYNC, {"username": f"{name}-on-server", "password": "pw"}, AccountTarget.PERSONAL)
    return user.id


def _tracks(session, *names: str, indexed=()) -> list[str]:
    """Tracks of the library; the ``indexed`` ones have an id on the sync provider."""
    ids = []
    for name in names:
        record = TrackRecord(title=name, artist="Artist", album="Album")
        session.add(record)
        session.commit()
        if name in indexed:
            add_identifiers(record.internal_id, [Identifier(type=sync_id_type(SYNC), value=f"s-{name}")], session)
            session.commit()
        ids.append(record.internal_id)
    return ids


def _playlist(session, user_id: str, name: str, track_ids: list[str]) -> str:
    playlist = Playlist(user_id=user_id, name=name)
    session.add(playlist)
    session.flush()
    playlist_store.append_tracks(session, playlist.id, track_ids)
    session.commit()
    return playlist.id


def _push(session, *providers):
    return [r for r in playlist_sync.push_all(session, FakeRegistry(syncs=list(providers)))]


def test_a_playlist_is_created_on_the_owners_own_account_with_the_tracks_the_provider_holds(session, sync):
    alice = _user(session, "alice")
    ids = _tracks(session, "One", "Two", "Three", indexed=("One", "Three"))
    playlist_id = _playlist(session, alice, "Drive", ids)

    (report,) = _push(session, sync)

    assert sync.calls == [("create", "Drive", ["s-One", "s-Three"])]
    assert sync.playlists["remote-1"]["account"] == "alice-on-server"
    assert (report["users"], report["created"], report["tracks_not_indexed"], report["failed"]) == (1, 1, 1, 0)
    row = session.exec(select(PlaylistSync)).one()
    assert (row.playlist_id, row.sync_provider, row.remote_id, row.user_id) == (playlist_id, SYNC, "remote-1", alice)


def test_an_unchanged_playlist_costs_no_call(session, sync):
    alice = _user(session, "alice")
    _playlist(session, alice, "Drive", _tracks(session, "One", indexed=("One",)))
    _push(session, sync)
    sync.calls.clear()

    (report,) = _push(session, sync)

    assert sync.calls == []
    assert (report["unchanged"], report["created"], report["updated"]) == (1, 0, 0)


def test_a_new_track_a_rename_and_a_reorder_are_pushed_as_the_whole_content(session, sync):
    alice = _user(session, "alice")
    one, two = _tracks(session, "One", "Two", indexed=("One", "Two"))
    playlist_id = _playlist(session, alice, "Drive", [one])
    _push(session, sync)
    sync.calls.clear()

    playlist_store.append_tracks(session, playlist_id, [two])
    session.commit()
    _push(session, sync)
    playlist_store.reorder(session, playlist_id, [two, one])
    session.commit()
    _push(session, sync)
    session.get(Playlist, playlist_id).name = "Night"
    session.commit()
    _push(session, sync)

    assert sync.calls == [
        ("update", "remote-1", "Drive", ["s-One", "s-Two"]),
        ("update", "remote-1", "Drive", ["s-Two", "s-One"]),
        ("update", "remote-1", "Night", ["s-Two", "s-One"]),
    ]


def test_a_track_indexed_later_joins_the_copy_on_a_later_run(session, sync):
    alice = _user(session, "alice")
    one, two = _tracks(session, "One", "Two", indexed=("One",))
    _playlist(session, alice, "Drive", [one, two])
    _push(session, sync)

    add_identifiers(two, [Identifier(type=sync_id_type(SYNC), value="s-Two")], session)
    session.commit()
    (report,) = _push(session, sync)

    assert sync.calls[-1] == ("update", "remote-1", "Drive", ["s-One", "s-Two"])
    assert report["tracks_not_indexed"] == 0


def test_the_copy_of_a_deleted_playlist_is_deleted_on_the_provider(session, sync):
    alice = _user(session, "alice")
    playlist_id = _playlist(session, alice, "Drive", _tracks(session, "One", indexed=("One",)))
    _push(session, sync)
    playlist = session.get(Playlist, playlist_id)
    playlist_store.delete_playlist(session, playlist)
    session.commit()
    assert len(session.exec(select(PlaylistSync)).all()) == 1  # the row outlives the playlist

    (report,) = _push(session, sync)

    assert sync.playlists == {}
    assert session.exec(select(PlaylistSync)).all() == []
    assert report["deleted"] == 1


def test_a_copy_the_user_deleted_on_the_server_is_created_again(session, sync):
    alice = _user(session, "alice")
    _playlist(session, alice, "Drive", _tracks(session, "One", indexed=("One",)))
    _push(session, sync)
    sync.playlists.clear()  # deleted on the server by hand
    session.get(Playlist, session.exec(select(Playlist.id)).one()).name = "Drive 2"
    session.commit()

    (report,) = _push(session, sync)

    assert (report["created"], report["updated"], report["failed"]) == (1, 0, 0)
    assert list(sync.playlists) == ["remote-2"]
    assert session.exec(select(PlaylistSync)).one().remote_id == "remote-2"


def test_a_copy_already_gone_on_the_server_is_forgotten_when_its_playlist_is_deleted(session, sync):
    alice = _user(session, "alice")
    playlist_id = _playlist(session, alice, "Drive", _tracks(session, "One", indexed=("One",)))
    _push(session, sync)
    sync.playlists.clear()
    playlist_store.delete_playlist(session, session.get(Playlist, playlist_id))
    session.commit()

    (report,) = _push(session, sync)

    assert session.exec(select(PlaylistSync)).all() == [] and report["deleted"] == 1 and report["failed"] == 0


def test_every_user_is_pushed_with_their_own_account_and_one_without_is_skipped(session, sync):
    alice = _user(session, "alice")
    bob = _user(session, "bob")
    carol = _user(session, "carol", account=False)
    ids = _tracks(session, "One", indexed=("One",))
    for owner, name in ((alice, "A"), (bob, "B"), (carol, "C")):
        _playlist(session, owner, name, ids)

    (report,) = _push(session, sync)

    accounts = {entry["name"]: entry["account"] for entry in sync.playlists.values()}
    assert accounts == {"A": "alice-on-server", "B": "bob-on-server"}
    assert (report["users"], report["skipped_no_account"], report["created"]) == (2, 1, 2)


def test_a_shared_account_is_never_used_for_a_users_playlists(session, sync):
    owner = _user(session, "dave", account=False)
    CredentialStore(session, user_id=None).save_credentials(
        SYNC, {"username": "shared", "password": "pw"}, AccountTarget.SHARED)
    _playlist(session, owner, "D", _tracks(session, "One", indexed=("One",)))

    (report,) = _push(session, sync)

    assert sync.calls == [] and report["skipped_no_account"] == 1


def test_a_provider_that_does_not_keep_playlists_is_not_touched(session, sync):
    alice = _user(session, "alice")
    _playlist(session, alice, "Drive", _tracks(session, "One", indexed=("One",)))
    plain = FakeSyncProvider("plain")

    reports = _push(session, plain, sync)

    assert [r["sync_provider"] for r in reports] == [SYNC]


def test_a_failing_provider_is_reported_with_its_code_and_the_next_run_tries_again(session, sync):
    alice = _user(session, "alice")
    bob = _user(session, "bob")
    ids = _tracks(session, "One", indexed=("One",))
    _playlist(session, alice, "A", ids)
    _playlist(session, bob, "B", ids)
    sync.fail = True

    reports = _push(session, sync)
    result = playlist_sync.push_result(reports)

    assert (result["status"], result["error_code"], result["failed"], result["created"]) == (
        "error", "sync_provider_unreachable", 2, 0)
    assert result["failed_providers"] == [SYNC]
    assert session.exec(select(PlaylistSync)).all() == []

    sync.fail = False
    ok = playlist_sync.push_result(_push(session, sync))
    assert (ok["status"], ok["created"], ok["failed"]) == ("ok", 2, 0)


def test_the_result_sums_the_providers_and_keeps_one_report_each(session, sync):
    alice = _user(session, "alice")
    _playlist(session, alice, "A", _tracks(session, "One", indexed=("One",)))

    result = playlist_sync.push_result(_push(session, sync))

    assert result["status"] == "ok"
    assert [result[k] for k in ("created", "updated", "unchanged", "deleted", "tracks_not_indexed", "failed")] == [
        1, 0, 0, 0, 0, 0]
    assert [r["sync_provider"] for r in result["providers"]] == [SYNC]


def test_the_hash_depends_on_the_name_and_the_ordered_ids():
    assert playlist_sync.content_hash("A", ["1", "2"]) == playlist_sync.content_hash("A", ["1", "2"])
    assert len({playlist_sync.content_hash("A", ["1", "2"]), playlist_sync.content_hash("B", ["1", "2"]),
                playlist_sync.content_hash("A", ["2", "1"]), playlist_sync.content_hash("A", [])}) == 4


def test_a_track_with_two_files_uses_one_id(session):
    (track,) = _tracks(session, "One")
    for value in ("s-b", "s-a"):
        add_identifiers(track, [Identifier(type=sync_id_type(SYNC), value=value)], session, replace_stale_sync=False)
    session.commit()

    assert playlist_sync.sync_ids_of(session, SYNC, [track]) == {track: "s-a"}
    assert playlist_sync.sync_ids_of(session, SYNC, []) == {}
    assert session.exec(select(PlaylistTrack)).all() == []
