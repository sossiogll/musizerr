"""
The download matrix: request provider × identifier types the configured
downloaders accept × tracks with several ids of the same provider.

Real case (29 Sep): every immediate failure came from ``POST /download/track``
on a row of a non-preferred metadata provider. The task received the
REQUEST's identifiers (that provider's id + ISRC), not the ones the canonical
track already had; the worker enriched only tracks with at most one
identifier; the downloader that accepts only the preferred provider's ids was
never tried, and the fuzzy phase "tried" every downloader because
``hasattr(provider, "download_fuzzy")`` is always true — a definitive failure
in 0.2–3 s. Every success came from a preferred-provider row or from
``/downloads/{id}/retry``, which rebuilds the track from the database.

Each case goes through the real path: ``enqueue_track`` (what the task
receives) → ``download_task`` (enrichment, resolver) → the downloader.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from core.download_resolver import DownloadResolver
from musizeer_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from musizeer_sdk.interfaces import DownloadProvider, MetadataProvider
from musizeer_sdk.models import Track
from musizeer_sdk.context import StaticProviderContext
from core.provider_cooldown import NoCooldown
from core.provider_slots import UnlimitedSlots
from db.models import Download
from db.track_store import add_identifiers, find_or_create_track
from services import downloads
from services.config_manager import ConfigManager
from workers import tasks

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeRegistry

ISRC_VALUE = "USSM10011897"
DEAD_ID, LIVE_ID = "p-dead", "p-live"


def _ident(provider: str, value: str) -> Identifier:
    return Identifier(type=PROVIDER_ID, value=value, provider=provider)


def _row(provider: str, provider_id: str, *, isrc: bool = True) -> Track:
    ids = [_ident(provider, provider_id)]
    if isrc:
        ids.append(Identifier(type=ISRC, value=ISRC_VALUE))
    return Track(title="Dream On", artist="Aerosmith", album="Aerosmith", duration_ms=268_000,
                 provider=provider, provider_id=provider_id, external_ids=ids)


class _Downloader(DownloadProvider):
    """Accepts the identifier types it is given; has the recordings in
    ``available`` (identifier values) and, with ``fuzzy``, finds any track
    by its metadata."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str, accepts: set[str], *, available: set[str] = frozenset(), fuzzy: bool = False) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self.supported_identifier_types = set(accepts)
        self.supports_fuzzy = fuzzy
        self._available = set(available)
        self.calls: list[tuple[str, str]] = []

    def supports_identifier(self, identifier):
        return identifier.storage_type() in self.supported_identifier_types

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def _write(self, workspace: str) -> str:
        (Path(workspace) / "song.ogg").write_bytes(b"audio")
        return "song.ogg"

    def download_by_id(self, identifier, workspace, session):
        self.calls.append(("id", identifier.value))
        return self._write(workspace) if identifier.value in self._available else None

    def download_fuzzy(self, track, workspace, session):
        self.calls.append(("fuzzy", track.title))
        return self._write(workspace) if self.supports_fuzzy else None


class _NoFuzzyOverride(_Downloader):
    """A downloader that inherits the contract's ``download_fuzzy`` (returns
    None): it must never count as tried by the fuzzy phase."""

    download_fuzzy = DownloadProvider.download_fuzzy


class _Lookup(MetadataProvider):
    """A metadata provider that finds the recording by ISRC (its id: ``found``)."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "primary", found: str = LIVE_ID) -> None:
        super().__init__(StaticProviderContext())
        self.name = name
        self.label = name
        self._found = found
        self.lookups = 0

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def supports_isrc_lookup(self):
        return True

    def find_by_isrc(self, isrc, session):
        self.lookups += 1
        return _row(self.name, self._found) if isrc == ISRC_VALUE else None


@pytest.fixture
def engine(monkeypatch):
    eng = new_engine()
    monkeypatch.setattr(tasks, "engine", eng)
    return eng


@pytest.fixture
def config(engine, monkeypatch):
    cm = ConfigManager(engine=engine)
    monkeypatch.setattr(downloads, "get_config", lambda: cm)
    monkeypatch.setattr(tasks, "get_config", lambda: cm)
    return cm


@pytest.fixture
def worker(engine, config, monkeypatch):
    """Runs ``download_task`` in-process against ``registry``; returns the
    task's result and what the task was queued with."""
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "RedisProviderSlots", lambda **kw: UnlimitedSlots())
    monkeypatch.setattr(tasks, "RedisProviderCooldown", lambda **kw: NoCooldown())
    monkeypatch.setattr(tasks, "auth_manager_for", lambda store: FakeAuthManager())
    monkeypatch.setattr(tasks, "resolve_storage_provider", lambda registry, name: (object(), "disk"))
    monkeypatch.setattr(tasks, "_store_download", lambda result, workspace, track, *a, **k: f"lib/{track.title}.ogg")

    def run(request_row: Track, registry, *, canonical: list[Track] = ()) -> tuple[dict, dict]:
        monkeypatch.setattr(tasks, "get_registry", lambda: registry)
        config.update({"download_provider_order": [p.name for p in registry.all_download()]})
        with Session(engine) as session:
            for known in canonical:
                record = find_or_create_track(known, session)
                add_identifiers(record.internal_id, known.external_ids, session)
                session.commit()
            result = downloads.enqueue_track(request_row, session, registry=registry)
        assert result.status == downloads.QUEUED
        call = queued[-1]
        return tasks.download_task.run(track_data=call["track_data"], download_id=call["download_id"]), call

    return run


def _status(engine, download_id: str) -> str:
    with Session(engine) as session:
        return session.get(Download, download_id).status


# ------------------------------------------------------------------
# What the task receives
# ------------------------------------------------------------------

def test_the_task_receives_the_canonical_identifiers_not_only_the_requests(engine, config, monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))
    with Session(engine) as session:
        find_or_create_track(_row("primary", DEAD_ID), session)
        add_identifiers(find_or_create_track(_row("primary", LIVE_ID), session).internal_id, [_ident("primary", LIVE_ID)], session)
        session.commit()
        downloads.enqueue_track(_row("secondary", "s-1"), session, registry=FakeRegistry())

    ids = {(i["type"], i["value"]) for i in queued[0]["track_data"]["external_ids"]}
    assert {("primary_id", DEAD_ID), ("primary_id", LIVE_ID), ("secondary_id", "s-1"), ("isrc", ISRC_VALUE)} <= ids
    with Session(engine) as session:
        assert queued[0]["track_data"]["internal_id"] == session.exec(select(Download)).one().track_id


# ------------------------------------------------------------------
# The matrix
# ------------------------------------------------------------------

REQUEST_PROVIDERS = ["primary", "secondary"]
# (accepted identifier types, available values, fuzzy)
DOWNLOADERS = {
    "preferred_ids_only": ({"primary_id"}, {LIVE_ID}, False),
    "isrc": ({"isrc"}, {ISRC_VALUE}, False),
    "request_ids_only": ({"secondary_id"}, {"s-1"}, False),
    "fuzzy_only": (set(), set(), True),
}
# Canonical track as the library knows it before the request:
# one preferred id, two (the first no longer downloadable), or none yet
# (only a metadata lookup by ISRC finds the preferred id).
CANONICAL = {
    "one_id": [_row("primary", LIVE_ID)],
    "two_ids_first_dead": [_row("primary", DEAD_ID), _row("primary", LIVE_ID)],
    "unknown_yet": [],
}


@pytest.mark.parametrize("canonical", sorted(CANONICAL))
@pytest.mark.parametrize("downloader", sorted(DOWNLOADERS))
@pytest.mark.parametrize("request_provider", REQUEST_PROVIDERS)
def test_download_matrix(engine, worker, request_provider, downloader, canonical):
    accepts, available, fuzzy = DOWNLOADERS[downloader]
    dl = _Downloader("dl", accepts, available=available, fuzzy=fuzzy)
    registry = FakeRegistry(metadata=[_Lookup("primary", LIVE_ID), _Lookup("secondary", "s-1")], downloads=[dl])
    if request_provider == "secondary":
        request = _row("secondary", "s-1")
    else:
        request = _row("primary", DEAD_ID if canonical == "two_ids_first_dead" else LIVE_ID)

    result, call = worker(request, registry, canonical=CANONICAL[canonical])

    assert result["status"] == ds.DOWNLOADED, (result, dl.calls)
    assert _status(engine, call["download_id"]) == ds.DOWNLOADED


def test_every_accepted_identifier_of_a_downloader_is_tried(engine, worker):
    """"Dream On" has two ids of the preferred provider: the first one is
    gone, the second one downloads."""
    dl = _Downloader("dl", {"primary_id"}, available={LIVE_ID})
    registry = FakeRegistry(downloads=[dl])

    result, _ = worker(_row("secondary", "s-1"), registry,
                       canonical=[_row("primary", DEAD_ID), _row("primary", LIVE_ID)])

    assert result["status"] == ds.DOWNLOADED
    assert [c for c in dl.calls if c[0] == "id"] == [("id", DEAD_ID), ("id", LIVE_ID)]


# ------------------------------------------------------------------
# Worker enrichment and the fuzzy capability
# ------------------------------------------------------------------

def test_the_worker_enriches_when_no_downloader_accepts_an_identifier_the_track_has(engine, worker):
    lookup = _Lookup()
    dl = _Downloader("dl", {"primary_id"}, available={LIVE_ID})

    result, _ = worker(_row("secondary", "s-1"), FakeRegistry(metadata=[lookup], downloads=[dl]))

    assert lookup.lookups == 1
    assert result["status"] == ds.DOWNLOADED


def test_the_worker_does_not_enrich_when_a_downloader_accepts_an_identifier_already_there(engine, worker):
    lookup = _Lookup()
    dl = _Downloader("dl", {"isrc"}, available={ISRC_VALUE})

    result, _ = worker(_row("secondary", "s-1"), FakeRegistry(metadata=[lookup], downloads=[dl]))

    assert lookup.lookups == 0
    assert result["status"] == ds.DOWNLOADED


def test_a_downloader_without_a_fuzzy_implementation_is_not_tried_by_the_fuzzy_phase():
    plain = _NoFuzzyOverride("plain", {"primary_id"})
    fuzzy = _Downloader("fuzzy", set(), fuzzy=True)
    resolver = DownloadResolver(FakeRegistry(downloads=[plain, fuzzy]), FakeAuthManager(), ["plain", "fuzzy"])
    tried: list[tuple[str, str]] = []

    import tempfile
    with tempfile.TemporaryDirectory() as workspace:
        result = resolver.resolve(_row("secondary", "s-1"), lambda name: workspace, on_trying=lambda name, method: tried.append((name, method)))

    assert result.success and result.provider == "fuzzy"
    assert tried == [("fuzzy", "fuzzy")]


def test_the_contract_declares_no_fuzzy_download_by_default():
    assert DownloadProvider.supports_fuzzy is False
