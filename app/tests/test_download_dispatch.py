"""
Il routing reale di Celery per ``download_task``: che il nome registrato
dal task e la coda in cui ``queue_download`` lo accoda restino allineati.
Nessun broker: ``celery.amqp.router.route`` risolve la coda da
``task_routes``/dalla dichiarazione del task senza aprire una connessione.
"""
from __future__ import annotations

import pytest

from services.config_manager import ConfigManager
from services import downloads
from workers.celery_app import celery

import workers.tasks  # noqa: F401 — registra "workers.tasks.download_task" su `celery`



@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


def test_queue_download_dispatches_a_registered_task_to_the_downloads_queue(config, monkeypatch):
    """Cattura la vera chiamata che ``queue_download`` fa a ``celery.send_task``
    (nome + opzioni), poi verifica DUE cose sul nome/opzioni REALI catturati,
    non su valori scritti a mano nel test: il nome e' un task registrato, e
    instradare quelle stesse opzioni tramite il router di Celery produce la
    coda 'downloads'. Se il nome del task o la sua coda derivassero, questo
    test si accorge di entrambi i casi."""
    monkeypatch.setattr(downloads, "get_config", lambda: config)

    captured = {}

    def _fake_send_task(name, kwargs=None, **opts):
        captured["name"] = name
        captured["kwargs"] = kwargs
        captured["opts"] = opts

    monkeypatch.setattr(celery, "send_task", _fake_send_task)

    downloads.queue_download(track_data={"title": "x"}, download_id="dl-1", user_id="u-1")

    assert captured["name"] in celery.tasks, (
        f"'{captured['name']}' non e' un task registrato su `celery` — il nome e' derivato "
        "rispetto al decoratore @celery.task che lo registra."
    )

    routed = celery.amqp.router.route(
        captured["opts"], captured["name"], args=(), kwargs=captured["kwargs"],
    )
    assert routed["queue"].name == "downloads", (
        f"'{captured['name']}' instrada sulla coda '{routed['queue'].name}', non 'downloads' — "
        "queue_download e il worker che lo esegue finirebbero su code diverse."
    )
