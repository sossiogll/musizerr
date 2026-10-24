"""
Pubblicazione degli aggiornamenti di progresso dei download su Redis
pub/sub. Il worker (sync) chiama ``publish_progress``; l'endpoint SSE
(``routes/system/progress.py``) si abbona allo stesso canale per
propagarli al frontend in tempo reale — ``REDIS_URL``/``PROGRESS_CHANNEL``
sono pubbliche proprio perche' condivise tra i due lati.
"""
from __future__ import annotations
import json
import logging

from sqlmodel import Session

from core import download_states as ds, environment
from db.engine import engine

logger = logging.getLogger(__name__)

REDIS_URL = environment.redis_url()
PROGRESS_CHANNEL = "musizeer:progress"


def publish_progress(
    download_id: str,
    status: str,
    track_title: str = "",
    track_id: str = "",
    **extra,
) -> None:
    """
    Chiamato da workers/tasks.py (sync) per notificare il frontend.
    Usa il client Redis sync standard.
    extra: campi aggiuntivi (es. step, provider_attempting, retry_count)
    """
    _publish(json.dumps({
        "download_id": download_id,
        "status": status,
        "track_title": track_title,
        "track_id": track_id,
        **extra,
    }))


def publish_download_progress(
    download_id: str,
    status: str,
    track_title: str,
    track_id: str | None,
    registry,
    **extra,
) -> None:
    """
    Come ``publish_progress``, più lo stato del FILE della traccia deciso
    ora dal server (``file_state``, e la sua ``phase``): ``status`` è il
    tentativo, ``file_state`` ciò che la riga mostra — ``downloaded`` per una
    traccia già in libreria, qualunque cosa succeda al tentativo (un nuovo
    tentativo forzato, un fallimento). La UI mostra ``file_state``, mai
    ``status`` da solo.
    """
    from services.track_state import file_state_now

    file_state = status
    try:
        with Session(engine) as session:
            file_state = file_state_now(session, track_id, registry) or status
    except Exception as exc:  # noqa: BLE001 — l'evento parte comunque, con lo stato del tentativo
        logger.warning("file state of %s not computed for its progress event: %s", track_id, exc)
    publish_progress(
        download_id, status, track_title, track_id or "",
        file_state=file_state, phase=ds.phase_of(file_state), **extra,
    )


def publish_event(event: dict) -> None:
    """Pubblica un evento qualunque sullo stesso canale (es. il progresso di
    un job di libreria, ``services/library_jobs.py``). Un evento con
    ``user_id`` arriva solo a quell'utente (``routes/system/progress.py``)."""
    _publish(json.dumps(event))


def _publish(payload: str) -> None:
    import redis as sync_redis
    client = sync_redis.from_url(REDIS_URL)
    try:
        client.publish(PROGRESS_CHANNEL, payload)
    finally:
        client.close()
