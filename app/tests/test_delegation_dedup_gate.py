"""
Copre un bug reale trovato durante l'audit dead-code/duplicazioni: prima di
questo fix, ``workers.maintenance._apply_delegation_status`` risolveva SEMPRE
i conflitti di importazione di un download delegato (cancellando la copia
"peggiore" via l'API del provider) senza controllare il toggle
``dedup_enabled`` (default: False, vedi core/settings_catalog.py) — cioè
musizerr cancellava file anche con la dedup esplicitamente disattivata.

Nessun provider reale qui (Golden Rule): un fake locale minimale implementa
solo le due capability opzionali coinvolte (poll_delegation/delete_file).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from services.config_manager import ConfigManager
from core import download_states as ds
from musizerr_sdk.delegation import DelegationStatus, FileRef, ImportConflict
from db.models import Download, TrackRecord
from tests.conftest import FakeDownloadProvider
from tests.database import new_engine
from tests.conftest import FakeRegistry
from tests.loopback import as_proxy
from workers import maintenance


class _FakeDedupDownloadProvider(FakeDownloadProvider):
    """Fake che implementa le capability opzionali di riconciliazione
    (poll_delegation/delete_file) con un vocabolario inventato — usato solo
    per dimostrare che _resolve_conflicts viene chiamata o no in base al
    toggle, non per testare un provider reale."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "fakededup") -> None:
        super().__init__(name)
        self.deleted: list[FileRef] = []

    def delete_file(self, ref: FileRef, session) -> bool:
        self.deleted.append(ref)
        return True


@pytest.fixture
def isolated_engine(monkeypatch):
    engine = new_engine()
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "publish_download_progress", lambda *a, **k: None)
    return engine


def _make_download(engine) -> str:
    with Session(engine) as session:
        track = TrackRecord(title="T", artist="A", album="Al")
        session.add(track)
        session.commit()
        session.refresh(track)
        dl = Download(track_id=track.internal_id, status=ds.DISPATCHED, delegation_handle="1")
        session.add(dl)
        session.commit()
        session.refresh(dl)
        return dl.id


def _conflict_status() -> DelegationStatus:
    incoming = FileRef(path="/new.flac", quality_lossless=True, external_id="new")
    existing = FileRef(path="/old.mp3", quality_lossless=False, external_id="old")
    conflict = ImportConflict(track_key="t", incoming=incoming, existing=existing)
    return DelegationStatus(state=ds.CONFLICT, conflicts=[conflict])


def test_conflict_not_resolved_when_dedup_disabled(isolated_engine):
    """Default (dedup_enabled=False): nessuna cancellazione via API, il
    download resta in CONFLICT (non terminale) invece di avanzare a IMPORTING."""
    settings_engine = new_engine()
    config = ConfigManager(engine=settings_engine)
    assert config.dedup_enabled is False  # default del catalogo

    provider = _FakeDedupDownloadProvider()
    download_id = _make_download(isolated_engine)

    terminal = maintenance._apply_delegation_status(
        download_id, as_proxy(provider), AuthSession.anonymous(), _conflict_status(), config, FakeRegistry(),
    )

    assert provider.deleted == []
    assert terminal is False
    with Session(isolated_engine) as session:
        record = session.get(Download, download_id)
        assert record.status == ds.CONFLICT


def test_conflict_resolved_when_dedup_enabled(isolated_engine):
    """Con dedup_enabled=True la policy qualità gira: il file peggiore viene
    cancellato via delete_file e lo stato avanza a IMPORTING."""
    settings_engine = new_engine()
    config = ConfigManager(engine=settings_engine)
    config.update({"dedup_enabled": True})
    assert config.dedup_enabled is True

    provider = _FakeDedupDownloadProvider()
    download_id = _make_download(isolated_engine)

    terminal = maintenance._apply_delegation_status(
        download_id, as_proxy(provider), AuthSession.anonymous(), _conflict_status(), config, FakeRegistry(),
    )

    assert [ref.external_id for ref in provider.deleted] == ["old"]
    assert terminal is False
    with Session(isolated_engine) as session:
        record = session.get(Download, download_id)
        assert record.status == ds.IMPORTING
