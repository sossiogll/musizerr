"""
Is a worker running for a queue: every worker process refreshes, for each
queue it consumes, a Redis key with a TTL (``start_heartbeat``, started when
the worker is ready); the key's presence means a worker of that queue was
alive less than ``HEARTBEAT_TTL_SECONDS`` ago. Downloads, library jobs and
maintenance run in separate processes (docs/workers.md): a job waits for the
jobs worker, whatever the others are doing.

It answers sooner than ``scheduler_stale`` (``routes/system/routines.py``),
which can only notice a missing worker once a routine is overdue by its
interval plus a grace period: a job waiting in the queue needs to know
within a minute or two.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from services.progress import REDIS_URL

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 20.0
HEARTBEAT_TTL_SECONDS = 60


def heartbeat_key(queue: str) -> str:
    return f"musizerr:worker:heartbeat:{queue}"


def beat(queues: Iterable[str], client=None) -> None:
    import redis as sync_redis
    own = client is None
    client = client or sync_redis.from_url(REDIS_URL)
    try:
        now = datetime.now(timezone.utc).isoformat()
        for queue in queues:
            client.set(heartbeat_key(queue), now, ex=HEARTBEAT_TTL_SECONDS)
    finally:
        if own:
            client.close()


def worker_alive(queue: str) -> Optional[bool]:
    """True if a worker of ``queue`` refreshed its heartbeat recently, False
    if none did, None if Redis cannot be asked (then nothing is known)."""
    import redis as sync_redis
    try:
        client = sync_redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
        try:
            return bool(client.exists(heartbeat_key(queue)))
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — Redis down: unknown, never an error
        logger.debug("worker heartbeat unreadable: %s", exc)
        return None


def start_heartbeat(queues: Iterable[str]) -> threading.Thread:
    """A daemon thread that refreshes the heartbeat of ``queues`` for the
    process's life."""
    stop = threading.Event()
    queues = list(queues)

    def loop() -> None:
        while not stop.is_set():
            try:
                beat(queues)
            except Exception as exc:  # noqa: BLE001 — retried at the next interval
                logger.warning("worker heartbeat failed: %s", exc)
            stop.wait(HEARTBEAT_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, name="worker-heartbeat", daemon=True)
    thread.start()
    return thread
