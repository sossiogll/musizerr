"""
Cinque setting del catalogo (http_timeout, download_timeout,
token_expiry_buffer, download_soft_time_limit_seconds,
download_hard_time_limit_seconds) erano dichiarati e modificabili dalla UI ma
letti da costanti/env fissate all'import — cambiarli in UI non aveva alcun
effetto senza un restart. Questi test coprono i tre punti che ora leggono a
runtime da ConfigManager, seguendo lo stesso pattern (engine SQLite in-memory
dedicato) di test_config_manager.py e test_delegation_webhook.py.

Provider-agnostico: nessun nome di provider reale nei dati di test.
"""
from __future__ import annotations

import time

import pytest

from core.auth_manager import AuthManager
from services.config_manager import ConfigManager



@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


# ---------------------------------------------------------------------------
# (a) http_timeout: cambiarlo su un ConfigManager isolato cambia cosa
# risponde l'accessor, senza ri-importare nulla.
# ---------------------------------------------------------------------------


def test_http_timeout_accessor_reflects_a_config_change_without_reimport(config):
    assert config.http_timeout == 10  # default di catalogo

    config.set("http_timeout", 42)

    assert config.http_timeout == 42


def test_download_timeout_accessor_reflects_a_config_change_without_reimport(config):
    assert config.download_timeout == 60  # default di catalogo

    config.set("download_timeout", 120)

    assert config.download_timeout == 120


# ---------------------------------------------------------------------------
# (b) Un AuthManager costruito dall'host (services.auth.auth_manager_for)
# onora il token_expiry_buffer configurato in quel momento, non una costante.
# ---------------------------------------------------------------------------


def test_expiry_check_honours_a_changed_token_expiry_buffer(config, monkeypatch):
    import services.auth as host_auth

    monkeypatch.setattr(host_auth, "get_config", lambda: config)

    now = time.time()
    # Token che scade fra 30s: con il buffer di default (60s) è già "scaduto"
    # (buffer maggiore del tempo residuo), con un buffer di 5s non ancora.
    token_data = {"obtained_at": now - 3570, "expires_in": 3600}  # scade fra 30s

    config.set("token_expiry_buffer", 60)
    assert host_auth.auth_manager_for(credential_store=None)._is_expired(token_data) is True

    config.set("token_expiry_buffer", 5)
    assert host_auth.auth_manager_for(credential_store=None)._is_expired(token_data) is False


# ---------------------------------------------------------------------------
# (c) queue_download passa a send_task i limiti CORRENTI di config, non
# quelli (rimossi) fissati al decoratore all'import.
# ---------------------------------------------------------------------------


def test_queue_download_applies_the_configured_soft_and_hard_time_limits(config, monkeypatch):
    from services import downloads
    from workers.celery_app import celery

    monkeypatch.setattr(downloads, "get_config", lambda: config)

    config.update({
        "download_soft_time_limit_seconds": 111,
        "download_hard_time_limit_seconds": 222,
    })

    calls = []
    monkeypatch.setattr(
        celery, "send_task",
        lambda name, kwargs=None, **opts: calls.append((name, kwargs, opts)),
    )

    downloads.queue_download(track_data={"title": "x"}, download_id="dl-1", user_id="u-1")

    assert len(calls) == 1
    name, kwargs, opts = calls[0]
    assert name == "workers.tasks.download_task"
    assert kwargs == {"track_data": {"title": "x"}, "download_id": "dl-1", "user_id": "u-1", "force": False}
    # The queue comes from the route table, the same for every way of queuing it.
    from workers.celery_app import QUEUE_DOWNLOADS, queue_of
    assert "queue" not in opts and queue_of(name) == QUEUE_DOWNLOADS
    assert opts["soft_time_limit"] == 111
    assert opts["time_limit"] == 222


def test_queue_download_picks_up_a_config_change_between_two_calls(config, monkeypatch):
    """Nessun re-import: il secondo enqueue vede subito il nuovo limite."""
    from services import downloads
    from workers.celery_app import celery

    monkeypatch.setattr(downloads, "get_config", lambda: config)

    calls = []
    monkeypatch.setattr(
        celery, "send_task",
        lambda name, kwargs=None, **opts: calls.append(opts),
    )

    config.update({"download_soft_time_limit_seconds": 10, "download_hard_time_limit_seconds": 20})
    downloads.queue_download(track_data={}, download_id="dl-1", user_id=None)

    config.update({"download_soft_time_limit_seconds": 999, "download_hard_time_limit_seconds": 1000})
    downloads.queue_download(track_data={}, download_id="dl-2", user_id=None)

    assert calls[0]["soft_time_limit"] == 10
    assert calls[1]["soft_time_limit"] == 999
