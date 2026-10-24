"""
Copre la riconciliazione self-healing dei download delegati
(``workers.maintenance.reconcile_delegation`` / ``sweep_delegations``) in modo
GENERICO — Golden Rule: nessun provider reale qui, solo fake minimali che
implementano i contratti opzionali di ``musizeer_sdk.interfaces``.

Tre cose vengono provate, tutte a livello di contratto/reconciler, mai di
un provider specifico:

1. Il giudizio sulla TRACCIA richiesta è dell'host: il provider elenca le
   tracce del pacchetto che hanno già un file (``list_delegated_tracks``),
   senza decidere, e il reconciler le abbina alla Track di dominio del record
   (``services.delegation``) prima di chiedere lo stato del pacchetto.
2. Un fallimento TRANSITORIO (auth che solleva, o poll_delegation che
   solleva — "il sistema esterno non ha risposto affatto") non finalizza
   MAI il record e non applica il max-age: si ri-accoda sempre. Un
   fallimento successivo può poi RECUPERARE (self-healing) non appena il
   sistema esterno torna raggiungibile.
3. Uno stato non terminale REALE (il sistema HA risposto, genuinamente in
   corso o senza alcun segnale di attività) resta soggetto al max-age
   normale — genuinamente in corso oltre il max-age fallisce comunque,
   così come nessun segnale di attività oltre il max-age.
4. ``sweep_delegations`` (rete di sicurezza periodica, beat) riprende un
   record delegato non terminale ORFANO (nessuna chain di riconciliazione
   viva) e NON duplica una chain il cui lock è ancora fresco.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from services.config_manager import ConfigManager
from core import download_states as ds
from musizeer_sdk.delegation import DelegationStatus
from db.models import Download, TrackRecord
from tests.database import new_engine
from tests.conftest import FakeDownloadProvider, FakeRedis, FakeRegistry, make_track
from workers import maintenance


# ------------------------------------------------------------------
# Fake download provider
# ------------------------------------------------------------------

class _TrackAwareProvider(FakeDownloadProvider):
    """Un delegato che risponde sul PACKAGE (``poll_delegation``) ed elenca le
    tracce che ha già (``list_delegated_tracks``) senza decidere quale fosse la
    richiesta: registra le chiamate per dimostrare cosa fa il reconciler."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "trackaware", status_sequence=None, present=None) -> None:
        super().__init__(name)
        self.calls: list[str] = []
        self.listed: list[str] = []
        self._sequence = list(status_sequence or [])
        self._present = list(present or [])

    def list_delegated_tracks(self, handle, session):
        self.listed.append(handle)
        return list(self._present)

    def poll_delegation(self, handle, session):
        self.calls.append(handle)
        if self._sequence:
            return self._sequence.pop(0)
        return None


class _UnreliableProvider(FakeDownloadProvider):
    """poll_delegation che solleva finché ``.reachable`` è False — simula un
    sistema esterno irraggiungibile che poi si riprende (self-healing)."""

    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def __init__(self, name: str = "unreliable", *, reachable: bool = False, final_status=None) -> None:
        super().__init__(name)
        self.reachable = reachable
        self.final_status = final_status
        self.calls = 0

    def poll_delegation(self, handle, session):
        self.calls += 1
        if not self.reachable:
            raise ConnectionError("system unreachable")
        return self.final_status


# ------------------------------------------------------------------
# Fixture
# ------------------------------------------------------------------

@pytest.fixture
def isolated_engine(monkeypatch):
    engine = new_engine()
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "publish_download_progress", lambda *a, **k: None)
    fake_redis = FakeRedis()
    monkeypatch.setattr(maintenance, "_redis_client", fake_redis)
    return engine, fake_redis


@pytest.fixture
def config():
    settings_engine = new_engine()
    return ConfigManager(engine=settings_engine)


def _make_download(
    engine, *, track=None, status: str = ds.DISPATCHED, handle: str = "1",
    provider_name: str = "trackaware", started_at=None,
) -> str:
    track = track or make_track()
    with Session(engine) as session:
        record = TrackRecord(
            internal_id=track.internal_id, title=track.title, artist=track.artist,
            album=track.album, duration_ms=track.duration_ms,
        )
        session.add(record)
        session.commit()
        dl = Download(
            track_id=record.internal_id, status=status, delegation_handle=handle,
            provider_used=provider_name, started_at=started_at,
        )
        session.add(dl)
        session.commit()
        session.refresh(dl)
        return dl.id


def _registry(*providers) -> FakeRegistry:
    return FakeRegistry(downloads=list(providers))


def _fake_requeue(monkeypatch):
    """Sostituisce apply_async con una cattura in memoria: nessuna chiamata
    reale al broker Celery/Redis, mai eseguita nei test."""
    calls: list[str] = []
    monkeypatch.setattr(
        maintenance.reconcile_delegation, "apply_async",
        lambda kwargs, countdown=None: calls.append(kwargs["download_id"]),
    )
    return calls


# ------------------------------------------------------------------
# 1. Giudizio per traccia: dell'host, sui dati del provider
# ------------------------------------------------------------------

def test_a_track_the_package_already_has_is_downloaded_whatever_the_provider_says_of_the_package(isolated_engine, config, monkeypatch):
    """Il provider elenca la traccia richiesta e, sul pacchetto, dice 'in corso':
    e' l'host a decidere, e la traccia e' scaricata (il poll del pacchetto non
    serve nemmeno)."""
    engine, _ = isolated_engine
    track = make_track(title="Requested Song", artist="An Artist", album="An Album", duration_ms=200_000)
    download_id = _make_download(engine, track=track, provider_name="trackaware")
    provider = _TrackAwareProvider(
        status_sequence=[DelegationStatus(state=ds.DOWNLOADING)],
        present=[make_track(title="Requested Song", artist="An Artist", album="", duration_ms=206_000)],
    )
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)

    result = maintenance.reconcile_delegation(download_id)

    assert result["status"] == ds.DOWNLOADED
    assert provider.listed == ["1"]
    assert provider.calls == []
    with Session(engine) as session:
        assert session.get(Download, download_id).status == ds.DOWNLOADED


def test_a_package_without_the_requested_track_is_judged_by_its_own_status(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    track = make_track(title="Requested Song", artist="An Artist", album="An Album")
    download_id = _make_download(engine, track=track, provider_name="trackaware")
    provider = _TrackAwareProvider(
        status_sequence=[DelegationStatus(state=ds.DOWNLOADING)],
        present=[make_track(title="Another Song", artist="An Artist", album="")],
    )
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    _fake_requeue(monkeypatch)

    result = maintenance.reconcile_delegation(download_id)

    assert result["status"] == ds.DOWNLOADING
    assert provider.calls == ["1"]


# ------------------------------------------------------------------
# 2. Transitorio: mai finalizzato, mai soggetto al max-age; poi recupera
# ------------------------------------------------------------------

def test_transient_unreachability_reschedules_without_max_age_then_recovers(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    config.update({"delegation_max_age_hours": 1, "delegation_max_wait_hours": 200,
                   "delegation_poll_interval_seconds": 30})
    very_old = datetime.now(timezone.utc) - timedelta(hours=100)  # ben oltre il max-age, entro l'attesa massima
    download_id = _make_download(engine, provider_name="unreliable", started_at=very_old)
    provider = _UnreliableProvider(reachable=False)
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    requeued = _fake_requeue(monkeypatch)

    result = maintenance.reconcile_delegation(download_id)

    assert result["status"] == "poll-failed"
    assert requeued == [download_id]
    with Session(engine) as session:
        record = session.get(Download, download_id)
        # Non FAILED nonostante i 100h di età: l'irraggiungibilità non dice
        # nulla sull'esito reale, quindi il max-age non si applica qui.
        assert record.status not in ds.TERMINAL

    # Self-healing: il sistema torna raggiungibile al prossimo tick.
    provider.reachable = True
    provider.final_status = DelegationStatus(state=ds.DOWNLOADED)

    result2 = maintenance.reconcile_delegation(download_id)

    assert result2["status"] == ds.DOWNLOADED
    with Session(engine) as session:
        record = session.get(Download, download_id)
        assert record.status == ds.DOWNLOADED


def test_transient_auth_failure_reschedules_without_finalizing(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    download_id = _make_download(engine, provider_name="trackaware")
    provider = _TrackAwareProvider()
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)

    class _BoomAuthManager:
        def __init__(self, credential_store=None):
            pass

        def authenticate(self, provider):
            raise RuntimeError("credentials not verifiable right now")

    monkeypatch.setattr(maintenance, "auth_manager_for", lambda store: _BoomAuthManager(credential_store=store))
    requeued = _fake_requeue(monkeypatch)

    result = maintenance.reconcile_delegation(download_id)

    assert result["status"] == "auth-failed"
    assert requeued == [download_id]
    assert provider.calls == []  # mai arrivato a poll_delegation
    with Session(engine) as session:
        record = session.get(Download, download_id)
        assert record.status not in ds.TERMINAL


# ------------------------------------------------------------------
# 3. Stato non terminale REALE: soggetto al max-age come sempre
# ------------------------------------------------------------------

def test_no_activity_signal_past_max_age_becomes_failed(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    config.update({"delegation_max_age_hours": 1, "delegation_poll_interval_seconds": 120})
    clock = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(maintenance, "_now", lambda: clock["now"])
    download_id = _make_download(engine, provider_name="trackaware", started_at=clock["now"])
    # Il sistema HA risposto (nessuna eccezione) ma non riporta alcuna
    # attività — è un esito reale, non ignoranza: le risposte consumano il
    # max-age, poll dopo poll.
    provider = _TrackAwareProvider(status_sequence=[DelegationStatus(state=ds.DISPATCHED, detail="no activity")] * 40)
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    _fake_requeue(monkeypatch)

    results = []
    for _ in range(31):  # 31 poll ogni 2 minuti: 62 minuti osservati
        clock["now"] += timedelta(seconds=120)
        results.append(maintenance.reconcile_delegation(download_id))

    assert all(r["status"] == ds.DISPATCHED for r in results[:-1])
    assert results[-1] == {"status": ds.FAILED, "detail": "max-age"}
    with Session(engine) as session:
        record = session.get(Download, download_id)
        assert record.status == ds.FAILED
        assert record.retriable is False  # ha risposto per un'ora senza esito
        assert record.delegation_answers == 31


def test_genuinely_in_flight_stays_non_terminal_and_reschedules(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    download_id = _make_download(engine, provider_name="trackaware")
    provider = _TrackAwareProvider(status_sequence=[DelegationStatus(state=ds.DOWNLOADING)])
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    requeued = _fake_requeue(monkeypatch)

    result = maintenance.reconcile_delegation(download_id)

    assert result["status"] == ds.DOWNLOADING
    assert requeued == [download_id]
    with Session(engine) as session:
        record = session.get(Download, download_id)
        assert record.status == ds.DOWNLOADING


# ------------------------------------------------------------------
# 4. sweep_delegations: riprende gli orfani, non duplica le chain vive
# ------------------------------------------------------------------

def test_sweep_resumes_orphan_and_skips_record_with_fresh_lock(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    orphan_id = _make_download(engine, provider_name="trackaware", handle="10")
    alive_id = _make_download(engine, provider_name="trackaware", handle="20")
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    resumed = _fake_requeue(monkeypatch)
    # Simula una chain reconcile_delegation già viva per `alive_id`.
    assert maintenance.start_reconciliation(alive_id, config) is True
    resumed.clear()

    result = maintenance.sweep_delegations()

    assert orphan_id in resumed
    assert alive_id not in resumed
    assert result["checked"] == 2
    assert result["resumed"] == 1


def _queue(monkeypatch) -> list[tuple[dict, int | None]]:
    """Like _fake_requeue, keeping the whole task (kwargs, countdown)."""
    tasks: list[tuple[dict, int | None]] = []
    monkeypatch.setattr(
        maintenance.reconcile_delegation, "apply_async",
        lambda kwargs, countdown=None: tasks.append((dict(kwargs), countdown)),
    )
    return tasks


def test_a_sweep_right_after_a_dispatch_does_not_start_a_second_chain(isolated_engine, config, monkeypatch):
    """The first tick is due only after the poll interval: the dispatch must
    already mark the chain alive, or the sweep in between resumes it."""
    engine, _ = isolated_engine
    config.update({"delegation_poll_interval_seconds": 120})
    download_id = _make_download(engine, provider_name="trackaware")
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    tasks = _queue(monkeypatch)

    maintenance.start_reconciliation(download_id, config)
    result = maintenance.sweep_delegations()

    assert result == {"status": "swept", "checked": 1, "resumed": 0}
    assert [(kw["download_id"], countdown) for kw, countdown in tasks] == [(download_id, 120)]


def test_the_download_task_marks_the_chain_alive_when_it_dispatches():
    """workers/tasks.py starts the chain through start_reconciliation (which
    takes the lock), never with a bare apply_async."""
    import inspect

    from workers import tasks

    source = inspect.getsource(tasks)
    assert "start_reconciliation(" in source
    assert "reconcile_delegation.apply_async" not in source


def test_extra_triggers_never_fork_the_chain(isolated_engine, config, monkeypatch):
    """A webhook push and a stale second chain (e.g. queued before an
    upgrade) do their poll, but only the chain holding the lock goes on."""
    engine, _ = isolated_engine
    download_id = _make_download(engine, provider_name="trackaware")
    provider = _TrackAwareProvider(status_sequence=[DelegationStatus(state=ds.DOWNLOADING)] * 10)
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    tasks = _queue(monkeypatch)

    maintenance.start_reconciliation(download_id, config)
    (chain_kwargs, _), = tasks
    tasks.clear()

    # A push event: a one-off run while the chain is alive.
    assert maintenance.reconcile_delegation(download_id)["detail"] == "chain-alive"
    # A chain that lost the lock to another one.
    assert maintenance.reconcile_delegation(download_id, chain="stale")["status"] == "superseded"
    assert tasks == []

    # The owner goes on, with its own token.
    assert maintenance.reconcile_delegation(**chain_kwargs)["detail"] == "re-queued"
    assert [kw for kw, _ in tasks] == [chain_kwargs]
    assert len(provider.calls) == 2  # the one-off and the owner polled; the stale chain did not


def test_a_poll_publishes_only_a_change_of_state(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    download_id = _make_download(engine, provider_name="trackaware", status=ds.DISPATCHED)
    provider = _TrackAwareProvider(status_sequence=[
        DelegationStatus(state=ds.DISPATCHED), DelegationStatus(state=ds.DISPATCHED),
        DelegationStatus(state=ds.DOWNLOADING), DelegationStatus(state=ds.DOWNLOADING),
    ])
    monkeypatch.setattr(maintenance, "get_registry", lambda: _registry(provider))
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    published: list[str] = []
    monkeypatch.setattr(maintenance, "publish_download_progress", lambda _id, state, *a, **k: published.append(state))
    _fake_requeue(monkeypatch)

    for _ in range(4):
        maintenance.reconcile_delegation(download_id)

    assert published == [ds.DOWNLOADING]


def test_sweep_ignores_terminal_records(isolated_engine, config, monkeypatch):
    engine, _ = isolated_engine
    _make_download(engine, provider_name="trackaware", handle="30", status=ds.DOWNLOADED)
    monkeypatch.setattr(maintenance, "get_config", lambda: config)
    resumed = _fake_requeue(monkeypatch)

    result = maintenance.sweep_delegations()

    assert resumed == []
    assert result["checked"] == 0
