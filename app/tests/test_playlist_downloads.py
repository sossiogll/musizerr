"""
Deleting a playlist along with the files it downloaded
(``services/playlist_downloads.py``, the task ``delete_playlist_tracks``): only
what nothing else holds and Musizerr downloaded for this user; everything else
stays, and a failing storage leaves the track as it was. Fake providers only.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from db import playlist_store
from db.models import Download, DownloadFollower, LibraryEntry, TrackRecord, User, UserFavorite
from services import playlist_downloads, playlists
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry, FakeStorageProvider, FakeSyncProvider


class _Storage(FakeStorageProvider):
    def __init__(self):
        super().__init__("fakestorage")
        self.broken: set[str] = set()

    def delete(self, relative_path, session):
        if relative_path in self.broken:
            raise OSError("storage unreachable")
        return super().delete(relative_path, session)


@pytest.fixture
def world(engine):
    storage, sync = _Storage(), FakeSyncProvider("fakesync")
    registry = FakeRegistry(storage=[storage], syncs=[sync])
    config = ConfigManager(engine=engine)
    config.update({"storage_provider": "fakestorage"})

    class World:
        pass

    w = World()
    w.engine, w.storage, w.sync, w.registry, w.config = engine, storage, sync, registry, config
    with Session(engine) as session:
        for name in ("alice", "bob"):
            session.add(User(id=name, username=name, password_hash="x"))
        session.commit()
    return w


def _track(w, name: str, *, by="alice", source="download", entry=True) -> str:
    """A track downloaded by ``by``, its file on the storage."""
    path = f"Artist/Album/{name}.flac"
    w.storage.files[path] = b"audio"
    with Session(w.engine) as session:
        record = TrackRecord(title=name, artist="Artist", album="Album")
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, requested_by=by, status=ds.DOWNLOADED, file_path=path))
        if entry:
            session.add(LibraryEntry(track_id=record.internal_id, file_path=path, file_format="flac",
                                     source=source, storage_provider="fakestorage"))
        session.commit()
        return record.internal_id


def _delete(w, track_ids, user="alice") -> dict:
    with Session(w.engine) as session:
        return playlist_downloads.delete_downloads(
            session, w.registry, w.config, FakeAuthManager(), user, track_ids)


def _left(w) -> tuple[int, int, int]:
    with Session(w.engine) as session:
        return (len(session.exec(select(Download)).all()), len(session.exec(select(LibraryEntry)).all()),
                len(w.storage.files))


def test_the_downloaded_files_go_with_their_library_rows(world):
    one, two = _track(world, "One"), _track(world, "Two")

    report = _delete(world, [one, two])

    assert (report["deleted"], report["kept"], report["failed"]) == (2, 0, 0)
    assert sorted(world.storage.deleted) == ["Artist/Album/One.flac", "Artist/Album/Two.flac"]
    assert _left(world) == (0, 0, 0)
    # The sync provider is told to look again.
    assert world.sync.scan_calls == 1


def test_a_download_without_a_library_entry_is_deleted_too(world):
    track = _track(world, "One", entry=False)

    assert _delete(world, [track])["deleted"] == 1
    assert _left(world) == (0, 0, 0)


def test_a_track_another_playlist_holds_stays(world):
    track = _track(world, "One")
    with Session(world.engine) as session:
        playlist = playlists.create(session, "bob", "Bob's")
        playlist_store.append_tracks(session, playlist.id, [track])
        session.commit()

    report = _delete(world, [track])

    assert (report["deleted"], report["kept"]) == (0, 1)
    assert _left(world) == (1, 1, 1)
    assert world.sync.scan_calls == 0


def test_a_favorite_of_anyone_stays(world):
    track = _track(world, "One")
    with Session(world.engine) as session:
        session.add(UserFavorite(user_id="bob", track_id=track))
        session.commit()

    assert _delete(world, [track])["kept"] == 1
    assert _left(world) == (1, 1, 1)


def test_a_file_somebody_else_asked_for_stays(world):
    track = _track(world, "One", by="bob")

    assert _delete(world, [track])["kept"] == 1
    assert _left(world) == (1, 1, 1)


def test_a_file_the_library_scan_found_stays(world):
    track = _track(world, "One", source="scan")

    assert _delete(world, [track])["kept"] == 1
    assert _left(world) == (1, 1, 1)


def test_a_track_with_no_file_of_ours_stays(world):
    with Session(world.engine) as session:
        record = TrackRecord(title="Only indexed", artist="Artist", album="Album")
        session.add(record)
        session.commit()
        track = record.internal_id

    assert _delete(world, [track])["kept"] == 1


def test_a_storage_that_fails_leaves_that_track_and_the_others_go(world):
    broken, fine = _track(world, "Broken"), _track(world, "Fine")
    world.storage.broken.add("Artist/Album/Broken.flac")

    report = _delete(world, [broken, fine])

    assert (report["deleted"], report["failed"]) == (1, 1)
    assert _left(world) == (1, 1, 1)
    with Session(world.engine) as session:
        assert [d.track_id for d in session.exec(select(Download))] == [broken]


def test_followers_of_a_deleted_download_go_with_it(world):
    track = _track(world, "One")
    with Session(world.engine) as session:
        download = session.exec(select(Download)).one()
        session.add(DownloadFollower(download_id=download.id, user_id="bob"))
        session.commit()

    assert _delete(world, [track])["deleted"] == 1
    with Session(world.engine) as session:
        assert session.exec(select(DownloadFollower)).all() == []


def test_a_track_listed_twice_is_deleted_once(world):
    track = _track(world, "One")

    assert _delete(world, [track, track])["deleted"] == 1


def test_without_a_storage_provider_nothing_is_deleted_and_it_says_why(world):
    track = _track(world, "One")
    world.registry._storage = []

    report = _delete(world, [track])

    assert (report["deleted"], report["failed"], report["error_code"]) == (0, 1, "no_storage_provider")
    assert _left(world) == (1, 1, 1)
