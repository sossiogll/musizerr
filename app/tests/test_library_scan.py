"""
Importazione della libreria esistente (``services.library_scan``): segue lo
storage provider configurato, riconosce le tracce con l'identità condivisa
di ``db.track_store`` e si segna come fatta solo a scansione completa.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from core.auth_manager import AuthError
from musizerr_sdk.interfaces import StorageStreamResult
from db.models import LibraryEntry, TrackIdentifierRecord, TrackRecord
from db.settings_store import SettingsRepository
from db.track_store import find_or_create_track
from services.library_scan import LIBRARY_SCANNED, scan_library_once
from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry, FakeStorageProvider, make_track, write_flac


class _LibraryStorage(FakeStorageProvider):
    """Storage che contiene i file di ``source`` (una cartella del test) e li
    rende come file nella cartella di lavoro o, con ``chunks=True``, come
    blocchi di byte."""

    def __init__(self, source: Path, *, name: str = "shelf", chunks: bool = False) -> None:
        super().__init__(name)
        self.source = source
        self.chunks = chunks
        self.streamed: list[str] = []

    def list_files(self, prefix, session):
        return iter(sorted(p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()))

    def stream(self, relative_path, workspace, session):
        self.streamed.append(relative_path)
        full = self.source / relative_path
        if self.chunks:
            return StorageStreamResult(chunks=iter([full.read_bytes()]), filename=full.name)
        shutil.copy(full, Path(workspace) / full.name)
        return StorageStreamResult(file=full.name, filename=full.name)


@pytest.fixture
def session():
    engine = new_engine()
    with Session(engine) as s:
        yield s


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    write_flac(root / "A" / "01 one.flac", title="One", artist="Artist", isrc="AAA111")
    write_flac(root / "A" / "02 two.flac", title="Two", artist="Artist")
    write_flac(root / ".musizerr-work" / "x" / "part.flac", title="Part", artist="Artist")
    (root / "A" / "cover.jpg").write_bytes(b"jpg")
    return root


def _scan(session, storage, tmp_path, *, configured: str | None = None, auth_manager=None):
    config = SimpleNamespace(
        storage_provider=configured or (storage.name if storage else None),
        workspace_dir=tmp_path / "work",
    )
    registry = FakeRegistry(storage=[storage] if storage else [])
    return scan_library_once(session, registry, config, auth_manager or FakeAuthManager())


def _entries(session) -> dict[str, LibraryEntry]:
    return {e.file_path: e for e in session.exec(select(LibraryEntry)).all()}


def _scanned(session) -> bool:
    return SettingsRepository(session).get_system(LIBRARY_SCANNED) is True


def test_the_scan_indexes_the_audio_files_of_the_configured_storage_provider(session, library, tmp_path, sent_tasks):
    count = _scan(session, _LibraryStorage(library), tmp_path)

    entries = _entries(session)
    assert count == 2
    assert set(entries) == {"A/01 one.flac", "A/02 two.flac"}  # niente cartelle nascoste, niente non-audio
    assert {e.storage_provider for e in entries.values()} == {"shelf"}
    assert {e.source for e in entries.values()} == {"scan"}
    assert {e.file_format for e in entries.values()} == {"flac"}
    assert _scanned(session)
    # The tracks just found may be liked elsewhere: the library index starts,
    # and the like matching follows it.
    assert [(name, kwargs) for name, kwargs, _ in sent_tasks] == [
        ("workers.routines.start", {"routine_key": "sync_library_index"})]


def test_the_scan_reads_tags_through_the_storage_stream_and_leaves_no_workspace(session, library, tmp_path):
    storage = _LibraryStorage(library, chunks=True)

    _scan(session, storage, tmp_path)

    one = session.get(TrackRecord, _entries(session)["A/01 one.flac"].track_id)
    assert (one.title, one.artist) == ("One", "Artist")
    assert sorted(storage.streamed) == ["A/01 one.flac", "A/02 two.flac"]
    assert list((tmp_path / "work").glob("*/*")) == []


def test_a_file_is_linked_to_the_track_the_shared_identity_already_knows(session, library, tmp_path):
    by_isrc = find_or_create_track(make_track("One (Remastered)", "Artist", isrc="AAA111"), session)
    # The file is a 3-second FLAC: a track of another duration would be
    # another recording (the fuzzy fallback checks the duration too).
    by_name = find_or_create_track(make_track("two", "ARTIST", duration_ms=3_000), session)

    _scan(session, _LibraryStorage(library), tmp_path)

    entries = _entries(session)
    assert entries["A/01 one.flac"].track_id == by_isrc.internal_id
    assert entries["A/02 two.flac"].track_id == by_name.internal_id
    assert len(session.exec(select(TrackRecord)).all()) == 2


def test_a_new_track_keeps_the_identifiers_of_its_tags(session, library, tmp_path):
    _scan(session, _LibraryStorage(library), tmp_path)

    track_id = _entries(session)["A/01 one.flac"].track_id
    ids = session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == track_id)).all()
    assert {(i.id_type, i.value) for i in ids} == {("isrc", "AAA111")}


def test_a_file_whose_tags_do_not_describe_a_track_is_indexed_unmatched(session, tmp_path):
    root = tmp_path / "library"
    write_flac(root / "untitled.flac", artist="Artist")
    (root / "noise.mp3").write_bytes(b"not audio")

    assert _scan(session, _LibraryStorage(root), tmp_path) == 2

    assert {e.track_id for e in _entries(session).values()} == {None}
    assert session.exec(select(TrackRecord)).all() == []


def test_a_file_the_storage_cannot_stream_is_indexed_unmatched(session, library, tmp_path):
    class _Broken(_LibraryStorage):
        def stream(self, relative_path, workspace, session):
            raise OSError("connection reset")

    assert _scan(session, _Broken(library), tmp_path) == 2
    assert {e.track_id for e in _entries(session).values()} == {None}


def test_files_already_in_the_library_are_left_alone(session, library, tmp_path):
    session.add(LibraryEntry(track_id=None, file_path="A/01 one.flac", file_format="flac",
                             source="download", storage_provider="shelf"))
    session.commit()
    storage = _LibraryStorage(library)

    assert _scan(session, storage, tmp_path) == 1
    assert storage.streamed == ["A/02 two.flac"]
    assert _entries(session)["A/01 one.flac"].source == "download"


def test_an_empty_library_is_looked_at_again_on_the_next_start(session, tmp_path):
    root = tmp_path / "library"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "readme.txt").write_text("x")
    storage = _LibraryStorage(root)

    assert _scan(session, storage, tmp_path) == 0
    assert not _scanned(session)

    write_flac(root / "one.flac", title="One", artist="Artist")
    assert _scan(session, storage, tmp_path) == 1
    assert _scanned(session)


def test_a_library_whose_files_are_all_known_is_scanned(session, library, tmp_path, sent_tasks):
    for rel in ("A/01 one.flac", "A/02 two.flac"):
        session.add(LibraryEntry(track_id=None, file_path=rel, file_format="flac",
                                 source="download", storage_provider="shelf"))
    session.commit()

    assert _scan(session, _LibraryStorage(library), tmp_path) == 0
    assert _scanned(session)
    assert sent_tasks == []  # nothing new: no like matching to queue


def test_a_scanned_library_is_not_scanned_again(session, library, tmp_path):
    SettingsRepository(session).set_system(LIBRARY_SCANNED, True)
    storage = _LibraryStorage(library)

    assert _scan(session, storage, tmp_path) is None
    assert storage.streamed == []
    assert _entries(session) == {}


def test_without_a_storage_provider_the_scan_waits_for_the_next_start(session, tmp_path):
    assert _scan(session, None, tmp_path, configured="missing") is None
    assert not _scanned(session)


def test_a_storage_provider_that_cannot_list_leaves_the_scan_for_the_next_start(session, tmp_path):
    assert _scan(session, FakeStorageProvider("shelf"), tmp_path) is None
    assert not _scanned(session)


def test_a_storage_without_credentials_leaves_the_scan_for_the_next_start(session, library, tmp_path):
    class _NoCredentials(FakeAuthManager):
        def authenticate(self, provider):
            raise AuthError("No credentials configured")

    assert _scan(session, _LibraryStorage(library), tmp_path, auth_manager=_NoCredentials()) is None
    assert not _scanned(session)


def test_an_interrupted_listing_is_resumed_by_the_next_start(session, library, tmp_path):
    class _Unreliable(_LibraryStorage):
        fail = True

        def list_files(self, prefix, session):
            files = list(super().list_files(prefix, session))
            if not self.fail:
                return iter(files)

            def interrupted():
                yield from files[:2]  # la cartella nascosta, poi "A/01 one.flac"
                raise ConnectionError("storage went away")
            return interrupted()

    storage = _Unreliable(library)
    assert _scan(session, storage, tmp_path) is None
    assert not _scanned(session)
    assert set(_entries(session)) == {"A/01 one.flac"}

    storage.fail = False
    assert _scan(session, storage, tmp_path) == 1
    assert set(_entries(session)) == {"A/01 one.flac", "A/02 two.flac"}
    assert _scanned(session)



def test_a_library_scanned_with_the_workspace_inside_it_stays_intact(session, library):
    """A storage whose root is the folder the host works in (the workspace
    inside the library): the scan lists the hidden work folder too, indexes
    only the audio files and leaves the library as it was."""
    storage = _LibraryStorage(library)
    config = SimpleNamespace(storage_provider=storage.name, workspace_dir=library / ".musizerr-work")

    count = scan_library_once(session, FakeRegistry(storage=[storage]), config, FakeAuthManager())

    assert count == 2
    assert {e.storage_provider for e in _entries(session).values()} == {"shelf"}
    assert None not in {e.track_id for e in _entries(session).values()}
    assert (library / "A" / "01 one.flac").exists()  # the library stays intact
    assert sorted(p.name for p in (library / ".musizerr-work").rglob("*") if p.is_file()) == ["part.flac"]
