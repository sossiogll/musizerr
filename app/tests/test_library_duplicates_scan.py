"""
Test del routine "library_duplicates_scan" (``workers/duplicates.py``):
candidati via list_library, clustering, conferma per fingerprint (via
SyncProvider.fetch_partial, per sync id — MAI per il path virtuale), keeper,
cache e idempotenza sui rescan — tutto con provider finti (Golden Rule).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from musizerr_sdk.auth import AuthSession
from services.config_manager import ConfigManager
from core.identifiers import sync_id_type
from db.models import Download, DuplicateFingerprintCache, DuplicateGroup, DuplicateMember, TrackIdentifierRecord, TrackRecord
from tests.database import new_engine, seed_parents
from tests.conftest import (
    FakeIdentificationProvider,
    FakeRegistry,
    FakeSyncProvider,
    encode_identity_bytes,
    make_library_item,
)
from workers import duplicates


class _FakeAuthManager:
    """Autentica sempre in modo anonimo — basta ai fake provider di questo
    modulo, che ignorano il contenuto della sessione."""

    def __init__(self, credential_store=None) -> None:
        pass

    def authenticate(self, provider):
        return AuthSession.anonymous()


@pytest.fixture
def isolated_engine(monkeypatch):
    engine = new_engine()
    monkeypatch.setattr(duplicates, "engine", engine)
    monkeypatch.setattr(duplicates, "auth_manager_for", lambda store: _FakeAuthManager(credential_store=store))
    return engine


@pytest.fixture
def config():
    settings_engine = new_engine()
    cfg = ConfigManager(engine=settings_engine)
    cfg.update({
        "dedup_enabled": True,
        "identification_enabled": True,
        "sync_provider": "fakesync",
        "storage_provider": "fakestorage",
        "dedup_prefer_lossless": True,
    })
    return cfg


def _wire(monkeypatch, config, registry):
    monkeypatch.setattr(duplicates, "get_config", lambda: config)
    monkeypatch.setattr(duplicates, "get_registry", lambda: registry)


def _link_via_sync_id(engine, *, sync_name: str, sync_id: str, real_path: str) -> str:
    """Riproduce il link DIRETTO (track_identifiers) fra un sync_id e un
    Download 'downloaded' — il path VERO usato per la cancellazione, mai
    quello virtuale riportato da list_library. Ritorna il download_id."""
    with Session(engine) as session:
        track_id = f"t-{sync_id}"
        seed_parents(session, tracks=[track_id])
        download = Download(track_id=track_id, status="downloaded", file_path=real_path)
        session.add(download)
        session.add(TrackIdentifierRecord(
            track_id=track_id, id_type=sync_id_type(sync_name), value=sync_id,
        ))
        session.commit()
        session.refresh(download)
        return download.id


def _link_via_match(engine, *, title: str, artist: str, album: str, duration_ms: int, real_path: str) -> str:
    """Riproduce il fallback per MATCH (nessun track_identifiers per il sync
    id — es. mai streamato via musizerr): un Download 'downloaded' la cui
    TrackRecord combacia per titolo/artista/album col LibraryItem clusterizzato."""
    with Session(engine) as session:
        record = TrackRecord(title=title, artist=artist, album=album, duration_ms=duration_ms)
        session.add(record)
        session.flush()
        download = Download(track_id=record.internal_id, status="downloaded", file_path=real_path)
        session.add(download)
        session.commit()
        session.refresh(download)
        return download.id


# ------------------------------------------------------------------
# Gate generici
# ------------------------------------------------------------------

def test_skips_when_dedup_disabled(isolated_engine, config, monkeypatch):
    config.update({"dedup_enabled": False})
    _wire(monkeypatch, config, FakeRegistry())
    result = duplicates.scan_library_duplicates()
    assert result["status"] == "skipped"


def test_skips_when_identification_disabled(isolated_engine, config, monkeypatch):
    config.update({"identification_enabled": False})
    _wire(monkeypatch, config, FakeRegistry())
    result = duplicates.scan_library_duplicates()
    assert result["status"] == "skipped"


def test_skips_when_no_identification_provider_available(isolated_engine, config, monkeypatch):
    registry = FakeRegistry(
        syncs=[FakeSyncProvider("fakesync")],
        identification=[FakeIdentificationProvider("fakeident", available=False)],
    )
    _wire(monkeypatch, config, registry)
    result = duplicates.scan_library_duplicates()
    assert result["status"] == "skipped"


# ------------------------------------------------------------------
# Confirmed duplicate -> proposed group with quality keeper
# ------------------------------------------------------------------

def test_confirmed_duplicate_creates_proposed_group_with_best_quality_keeper(isolated_engine, config, monkeypatch):
    lossy = make_library_item(
        "Snow (Hey Oh)", "Red Hot Chili Peppers", "Stadium Arcadium",
        sync_id="sid-lossy", path="virtual/lossy.mp3", suffix="mp3", bitrate=320000, sample_rate=44100, size=1000,
    )
    lossless = make_library_item(
        "Snow (Hey Oh)", "Red Hot Chili Peppers", "Stadium Arcadium",
        sync_id="sid-lossless", path="virtual/lossless.flac", suffix="flac", bitrate=1000000, sample_rate=44100, size=2000,
    )
    sync = FakeSyncProvider("fakesync", library=[lossy, lossless])
    sync.files["sid-lossy"] = encode_identity_bytes("rec-1")
    sync.files["sid-lossless"] = encode_identity_bytes("rec-1")
    # Solo sid-lossy e' un Download musizerr (link diretto per sync id) -> e'
    # l'unico cancellabile, ed e' anche quello che perde per qualita': il
    # keeper puo' essere scelto in automatico (nessun file esterno da
    # cancellare per farlo).
    _link_via_sync_id(isolated_engine, sync_name="fakesync", sync_id="sid-lossy", real_path="real/lossy.mp3")

    registry = FakeRegistry(
        syncs=[sync],
        identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    result = duplicates.scan_library_duplicates()

    assert result["status"] == "done"
    assert result["groups_created"] == 1
    assert result["linked_via_sync_id"] == 1
    assert result["linked_none"] == 1
    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        assert groups[0].status == "proposed"
        members = session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == groups[0].id)
        ).all()
        assert len(members) == 2
        keeper = next(m for m in members if m.is_keeper)
        assert keeper.sync_id == "sid-lossless"
        loser = next(m for m in members if not m.is_keeper)
        assert loser.origin == "musizerr"
        assert loser.sync_id == "sid-lossy"


def test_resolve_download_for_item_direct_link_and_fallback_and_none(isolated_engine):
    """Unit-level (bypassa il pipeline di clustering, che richiederebbe tag
    ambigui fra loro): le tre uscite di ``resolve_download_for_item`` — link
    diretto per sync id (ignora i tag, anche se non combaciano), fallback per
    match (tag combacianti, nessun link diretto), e nessun link."""
    from db.duplicate_store import build_downloaded_track_index, resolve_download_for_item

    direct_download_id = _link_via_sync_id(
        isolated_engine, sync_name="fakesync", sync_id="sid-direct", real_path="real/direct.mp3",
    )
    fallback_download_id = _link_via_match(
        isolated_engine, title="Other Song", artist="Other Artist", album="Other Album",
        duration_ms=180_000, real_path="real/fallback.ogg",
    )

    item_direct = make_library_item("Whatever", "Whoever", "Whatever Album", sync_id="sid-direct")
    item_fallback = make_library_item(
        "Other Song", "Other Artist", "Other Album", sync_id="sid-fallback", duration_ms=180_000,
    )
    item_none = make_library_item("Nothing Like It", "Nobody", "Nothing Album", sync_id="sid-none")

    with Session(isolated_engine) as session:
        index = build_downloaded_track_index(session)
        d1, m1 = resolve_download_for_item(session, sync_name="fakesync", item=item_direct, downloaded_index=index)
        d2, m2 = resolve_download_for_item(session, sync_name="fakesync", item=item_fallback, downloaded_index=index)
        d3, m3 = resolve_download_for_item(session, sync_name="fakesync", item=item_none, downloaded_index=index)

    assert (m1, d1.id) == ("sync_id", direct_download_id)
    assert (m2, d2.id) == ("match", fallback_download_id)
    assert (m3, d3) == ("none", None)


def test_different_recording_ids_never_form_a_group(isolated_engine, config, monkeypatch):
    # Stesso titolo/artista/album/anno (cluster candidato) ma il fingerprint
    # dice che sono DUE registrazioni diverse -> mai un duplicato confermato.
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.mp3", suffix="mp3", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.mp3", suffix="mp3", size=2000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-a")
    sync.files["sid-b"] = encode_identity_bytes("rec-b")

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    result = duplicates.scan_library_duplicates()

    assert result["groups_created"] == 0
    with Session(isolated_engine) as session:
        assert session.exec(select(DuplicateGroup)).all() == []


def test_unknown_quality_results_in_needs_review_group(isolated_engine, config, monkeypatch):
    known = make_library_item("Song", "Artist", "Album", sync_id="sid-known", path="known.flac", suffix="flac", size=1000)
    unknown = make_library_item("Song", "Artist", "Album", sync_id="sid-unknown", path="unknown.bin", suffix="", size=2000)
    sync = FakeSyncProvider("fakesync", library=[known, unknown])
    sync.files["sid-known"] = encode_identity_bytes("rec-1")
    sync.files["sid-unknown"] = encode_identity_bytes("rec-1")
    _link_via_sync_id(isolated_engine, sync_name="fakesync", sync_id="sid-unknown", real_path="real/unknown.bin")

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    duplicates.scan_library_duplicates()

    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        assert groups[0].status == "needs_review"
        assert groups[0].reason == "unknown_quality"
        assert all(not m.is_keeper for m in session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == groups[0].id)
        ).all())


def test_external_origin_loser_forces_needs_review(isolated_engine, config, monkeypatch):
    # Il file di qualita' peggiore NON e' stato piazzato da musizerr (nessun
    # link, ne' diretto ne' per match) -> mai cancellato in automatico, il
    # gruppo resta needs_review.
    better = make_library_item("Song", "Artist", "Album", sync_id="sid-better", path="better.flac", suffix="flac",
                                bitrate=1000000, sample_rate=44100, size=1000)
    external_worse = make_library_item("Song", "Artist", "Album", sync_id="sid-external", path="external.mp3", suffix="mp3",
                                        bitrate=128000, sample_rate=44100, size=2000)
    sync = FakeSyncProvider("fakesync", library=[better, external_worse])
    sync.files["sid-better"] = encode_identity_bytes("rec-1")
    sync.files["sid-external"] = encode_identity_bytes("rec-1")
    # Nessun link per nessuno dei due: entrambi di origine sconosciuta.

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    duplicates.scan_library_duplicates()

    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        assert groups[0].status == "needs_review"
        assert groups[0].reason == "external_or_unknown_origin_would_be_deleted"


def test_ambiguous_shared_download_via_direct_sync_id_links_never_deletable(isolated_engine, config, monkeypatch):
    # Due sync_id DIVERSI (due file fisici diversi, confermato dal
    # fingerprint) linkati ENTRAMBI, per link DIRETTO, allo stesso track_id/
    # Download — es. lo storico streaming li ha associati in momenti diversi.
    # Un solo download.file_path reale non puo' essere attribuito con
    # certezza a nessuno dei due: nessuno resta cancellabile.
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-x", path="x.mp3",
                           suffix="mp3", bitrate=128000, sample_rate=44100, size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-y", path="y.ogg",
                           suffix="ogg", bitrate=320000, sample_rate=44100, size=2000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-x"] = encode_identity_bytes("rec-1")
    sync.files["sid-y"] = encode_identity_bytes("rec-1")

    with Session(isolated_engine) as session:
        seed_parents(session, tracks=["shared-track"])
        download = Download(track_id="shared-track", status="downloaded", file_path="real/shared.audio")
        session.add(download)
        session.flush()
        session.add(TrackIdentifierRecord(track_id="shared-track", id_type=sync_id_type("fakesync"), value="sid-x"))
        session.add(TrackIdentifierRecord(track_id="shared-track", id_type=sync_id_type("fakesync"), value="sid-y"))
        session.commit()

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    result = duplicates.scan_library_duplicates()

    assert result["linked_via_sync_id"] == 2
    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        members = session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == groups[0].id)
        ).all()
        assert all(m.download_id is None for m in members)
        assert groups[0].status == "needs_review"


def test_ambiguous_match_fallback_never_makes_either_member_deletable(isolated_engine, config, monkeypatch):
    # Due file DIVERSI con tag troppo simili (stessa canzone, grafia diversa)
    # combaciano ENTRAMBI, via fallback, con l'UNICO Download musizerr noto —
    # caso reale trovato in verifica live ("Curre curre guaglió"/"guagliò").
    # Nessuno dei due deve risultare cancellabile: l'identificazione e' ambigua.
    a = make_library_item("Song Variant", "Artist", "Album", sync_id="sid-a", path="a.mp3",
                           suffix="mp3", bitrate=128000, sample_rate=44100, size=1000, duration_ms=200_000)
    b = make_library_item("Song Variant", "Artist", "Album", sync_id="sid-b", path="b.ogg",
                           suffix="ogg", bitrate=320000, sample_rate=44100, size=2000, duration_ms=200_000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1")
    sync.files["sid-b"] = encode_identity_bytes("rec-1")
    # Un solo Download reale in indice, che i tag di ENTRAMBI combaciano.
    _link_via_match(isolated_engine, title="Song Variant", artist="Artist", album="Album",
                     duration_ms=200_000, real_path="real/only-one-of-them.audio")

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    result = duplicates.scan_library_duplicates()

    assert result["linked_via_match"] == 2  # entrambi hanno "trovato" lo stesso download...
    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        # ...ma nessuno dei due resta collegato: mai cancellabile su un match ambiguo.
        members = session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == groups[0].id)
        ).all()
        assert all(m.download_id is None for m in members)
        assert all(m.origin == "unknown" for m in members)
        assert groups[0].status == "needs_review"
        assert groups[0].reason == "external_or_unknown_origin_would_be_deleted"


# ------------------------------------------------------------------
# Cache + idempotenza
# ------------------------------------------------------------------

def test_cache_avoids_refingerprinting_unchanged_files(isolated_engine, config, monkeypatch):
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.flac", suffix="flac", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.flac", suffix="flac", size=1000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1")
    sync.files["sid-b"] = encode_identity_bytes("rec-1")
    _link_via_sync_id(isolated_engine, sync_name="fakesync", sync_id="sid-b", real_path="real/b.flac")

    identification = FakeIdentificationProvider("fakeident", available=True)
    registry = FakeRegistry(syncs=[sync], identification=[identification])
    _wire(monkeypatch, config, registry)

    first = duplicates.scan_library_duplicates()
    assert first["fingerprinted"] == 2
    calls_after_first = identification.identify_calls

    second = duplicates.scan_library_duplicates()
    assert second["fingerprinted"] == 0
    assert identification.identify_calls == calls_after_first  # nessuna nuova chiamata

    with Session(isolated_engine) as session:
        cache_rows = session.exec(select(DuplicateFingerprintCache)).all()
        assert len(cache_rows) == 2


def test_rescan_does_not_duplicate_the_group(isolated_engine, config, monkeypatch):
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.flac", suffix="flac", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.mp3", suffix="mp3", size=2000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1")
    sync.files["sid-b"] = encode_identity_bytes("rec-1")
    _link_via_sync_id(isolated_engine, sync_name="fakesync", sync_id="sid-b", real_path="real/b.mp3")

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)

    duplicates.scan_library_duplicates()
    duplicates.scan_library_duplicates()
    duplicates.scan_library_duplicates()

    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1


def test_rescan_drops_member_that_disappeared_from_the_library(isolated_engine, config, monkeypatch):
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.flac", suffix="flac", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.mp3", suffix="mp3", size=2000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1")
    sync.files["sid-b"] = encode_identity_bytes("rec-1")
    _link_via_sync_id(isolated_engine, sync_name="fakesync", sync_id="sid-b", real_path="real/b.mp3")

    registry = FakeRegistry(
        syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)],
    )
    _wire(monkeypatch, config, registry)
    duplicates.scan_library_duplicates()

    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        assert len(session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == groups[0].id)
        ).all()) == 2

    # "b.mp3" e' sparito dalla libreria indicizzata (es. cancellato fuori dal
    # dedup) -> il prossimo scan lo rimuove e, restando un solo membro, il
    # gruppo non e' piu' un duplicato: dismesso in automatico.
    sync._library = [a]
    duplicates.scan_library_duplicates()

    with Session(isolated_engine) as session:
        groups = session.exec(select(DuplicateGroup)).all()
        assert len(groups) == 1
        assert groups[0].status == "dismissed"


def test_a_file_the_sync_provider_could_not_serve_is_retried_next_scan(isolated_engine, config, monkeypatch):
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.flac", suffix="flac", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.flac", suffix="flac", size=1000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1")  # sid-b: il server non lo serve (ancora)
    registry = FakeRegistry(syncs=[sync], identification=[FakeIdentificationProvider("fakeident", available=True)])
    _wire(monkeypatch, config, registry)

    first = duplicates.scan_library_duplicates()
    sync.files["sid-b"] = encode_identity_bytes("rec-1")
    second = duplicates.scan_library_duplicates()

    assert first["groups_created"] == 0
    assert second["fingerprinted"] == 1  # solo sid-b, sid-a viene dalla cache
    assert second["groups_created"] == 1


def test_a_new_confidence_threshold_applies_to_files_already_fingerprinted(isolated_engine, config, monkeypatch):
    a = make_library_item("Song", "Artist", "Album", sync_id="sid-a", path="a.flac", suffix="flac", size=1000)
    b = make_library_item("Song", "Artist", "Album", sync_id="sid-b", path="b.flac", suffix="flac", size=1000)
    sync = FakeSyncProvider("fakesync", library=[a, b])
    sync.files["sid-a"] = encode_identity_bytes("rec-1", score=0.6)
    sync.files["sid-b"] = encode_identity_bytes("rec-1", score=0.6)
    identification = FakeIdentificationProvider("fakeident", available=True)
    _wire(monkeypatch, config, FakeRegistry(syncs=[sync], identification=[identification]))
    config.update({"identification_confidence_threshold": 0.9})

    strict = duplicates.scan_library_duplicates()
    config.update({"identification_confidence_threshold": 0.5})
    calls = identification.identify_calls
    relaxed = duplicates.scan_library_duplicates()

    assert strict["groups_created"] == 0
    assert relaxed["groups_created"] == 1
    assert identification.identify_calls == calls  # dalla cache, nessun nuovo fingerprint
