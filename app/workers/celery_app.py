from __future__ import annotations
import logging
import os

from celery import Celery
from celery.signals import worker_init, worker_ready

from db import credential_cipher
from services.config_manager import ConfigManager
from services.log_redaction import install_log_redaction
from services.process_registry import ProcessRegistry
from services.provider_registry import ProviderRegistry

# Prima di tutto il resto: anche i log di Celery e di httpx nei task.
install_log_redaction()
logger = logging.getLogger(__name__)

# A worker (and beat, and the API that sends it tasks) refuses to start without the key that
# opens the stored credentials: a download must never run on credentials it cannot read. Here, at
# import, because the signal handlers of Celery log what they raise and go on (docs/credentials.md).
credential_cipher.require_credentials_key()

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery = Celery(
    "musizerr",
    broker=_REDIS_URL,
    backend=_REDIS_URL,
    include=["workers.tasks", "workers.maintenance", "workers.scheduler", "workers.duplicates"],
)

# ------------------------------------------------------------------
# Code e processi worker (vedi docs/workers.md)
#
# Tre code, ognuna consumata dal PROPRIO processo worker, così un lavoro
# lungo di un tipo non fa mai aspettare gli altri:
#
# - ``downloads``: i download (fino a decine di secondi l'uno, minuti con un
#   provider o uno storage lento). Concorrenza = ``max_concurrent_downloads``.
# - ``jobs``: i job di libreria chiesti da un utente ("Sincronizza i mi
#   piace", "Scarica i mi piace mancanti"), le azioni admin che aspettano
#   un esito (verifica full-sync) e l'aggiornamento della libreria dal sync
#   provider (routine ``sync_library_index``, minuti su una libreria grande) e
#   l'importazione della libreria esistente (routine ``import_existing_library``).
# - ``maintenance``: il tick dello scheduler, i routine, le chain di
#   riconciliazione (download delegati, like) e i task brevi accodati dagli
#   altri (unlike, like matching). È la coda di default: un task senza una
#   rotta qui sotto finisce in ``maintenance``, mai fra i download.
#
# Le rotte stanno TUTTE qui, per nome registrato: un task accodato per nome
# (``send_task``, come fanno services/) non vede l'opzione ``queue=`` del
# decoratore, la tabella sì.
# ------------------------------------------------------------------
QUEUE_DOWNLOADS = "downloads"
QUEUE_JOBS = "jobs"
QUEUE_MAINTENANCE = "maintenance"
QUEUES = (QUEUE_DOWNLOADS, QUEUE_JOBS, QUEUE_MAINTENANCE)

TASK_ROUTES = {
    "workers.tasks.download_task": {"queue": QUEUE_DOWNLOADS},
    "workers.tasks.sync_saved_tracks_for_user": {"queue": QUEUE_JOBS},
    "workers.tasks.library_download_sync": {"queue": QUEUE_JOBS},
    "workers.maintenance.verify_full_sync": {"queue": QUEUE_JOBS},
    # The routine ``sync_library_index`` (a run executes on the queue of its
    # routine's task, ``workers.scheduler.dispatch_routine``): listing and
    # indexing a large sync library takes minutes, never on the tick's worker.
    "workers.tasks.sync_library_index": {"queue": QUEUE_JOBS},
    # The routine ``import_existing_library``: reads the tags of every file of an existing library.
    "workers.tasks.import_existing_library": {"queue": QUEUE_JOBS},
}

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,          # il task viene rimosso dalla coda solo dopo il completamento
    worker_prefetch_multiplier=1, # un task alla volta per worker, rispetta max_concurrent
    task_routes=TASK_ROUTES,
    task_queues={name: {} for name in QUEUES},
    task_default_queue=QUEUE_MAINTENANCE,
)


def queue_of(task_name: str) -> str:
    """La coda su cui un task accodato per nome finisce (rotta o default)."""
    return TASK_ROUTES.get(task_name, {}).get("queue", QUEUE_MAINTENANCE)

# Celery sizes the prefork pool before the worker_init signal fires, so
# setting worker_concurrency there (as the rest of the per-startup config
# does) is too late and silently no-ops — the pool stays at Celery's default
# (one process per CPU core). Read it here instead, at module import time,
# which happens while the `celery worker` command is still parsing config,
# before the pool is built. It sizes the DOWNLOADS worker; the jobs and
# maintenance workers pass their own ``--concurrency`` (the command line
# wins over this setting, see docker-compose).
_startup_config = ConfigManager()
celery.conf.update(worker_concurrency=_startup_config.max_concurrent_downloads)

# ------------------------------------------------------------------
# Beat schedule — must be visible to a STANDALONE `celery beat` process too.
#
# ONE static entry, `workers.routines.tick` (workers/scheduler.py), for every
# routine, so changing a routine's interval needs no restart. Beat only ever
# needs to know "run tick every 60s";
# tick itself reads the CURRENT `enabled`/`interval_minutes` settings for
# every routine (core/routines.py) from the `settings` table on each run
# and decides what is actually due. Built here, at module import time (same
# pattern as worker_concurrency just above), so it is visible to BOTH a
# `celery worker -B` process (dev: beat embedded in the single worker) and a
# standalone `celery beat` process (prod: separate service).
#
# This IS the fix for the old "beat restart required" limitation: only the
# tick's OWN cadence (60s) is static now — enabling/disabling a routine or
# changing its interval takes effect within one tick, no restart of
# anything. See workers/scheduler.py for the read-fresh-settings logic.
celery.conf.beat_schedule = {
    "system-routines-tick": {
        "task": "workers.routines.tick",
        "schedule": 60.0,
    },
}

# ------------------------------------------------------------------
# Stato globale del worker — inizializzato una volta sola al startup
# ------------------------------------------------------------------

# The registry is NOT built here: this runs in the Celery parent, and the tasks run in the
# children it forks (services/process_registry.py). Every process builds its own, the first
# time its code asks for it.
_process_registry = ProcessRegistry()
_config: ConfigManager | None = None


# The queues THIS worker process consumes (``-Q``), known once Celery has
# set them up (before worker_init).
_consumed: frozenset[str] = frozenset()


def consumed_queues(sender) -> frozenset[str]:
    try:
        return frozenset(sender.app.amqp.queues.consume_from)
    except AttributeError:
        return frozenset()


@worker_init.connect
def _on_worker_init(sender=None, **kwargs) -> None:
    global _config, _consumed
    logger.info("Initializing worker state...")
    _config = ConfigManager()
    _consumed = consumed_queues(sender)

    # Each worker cleans up only after ITS predecessor: the work left in
    # flight on its own queue. A maintenance worker restarting must never
    # fail the downloads the downloads worker is running right now.
    if QUEUE_DOWNLOADS in _consumed:
        reaped = _reap_orphaned_running_downloads()
        if reaped:
            logger.warning("Reaped %d orphaned 'running' download(s) from previous worker.", reaped)
    if QUEUE_JOBS in _consumed:
        reaped_jobs = _reap_orphaned_library_jobs()
        if reaped_jobs:
            logger.warning("Reaped %d orphaned 'running' library job(s) from previous worker.", reaped_jobs)

    # NOTE: beat_schedule is built at MODULE level (above), not here — see
    # the comment there. worker_init only fires in worker processes, never
    # in a standalone `celery beat` process, so a schedule assigned here
    # would be invisible to it.
    logger.info("Worker ready — queues=%s", ",".join(sorted(_consumed)) or "?")


def _reap_orphaned_running_downloads() -> int:
    """
    Mark any Download row stuck in 'running' as 'failed'.

    These come from previous worker processes killed mid-task (deploy, OOM,
    crash) before they could write the final status. Celery acked the
    message on the dead process, so nothing will ever resume them.

    Runs only in the worker that consumes the downloads queue (one process,
    see docs/workers.md). With several replicas of it this would race
    against legitimately in-flight tasks; gate it on
    `celery.control.inspect().active()` or a heartbeat timestamp first.
    """
    from datetime import datetime, timezone
    from sqlmodel import select
    from sqlmodel import Session
    from db.engine import engine
    from db.models import Download
    from core import download_states as ds

    try:
        with Session(engine) as session:
            stuck = session.exec(
                select(Download).where(Download.status.in_(tuple(ds.IN_FLIGHT)))
            ).all()
            if not stuck:
                return 0
            now = datetime.now(timezone.utc)
            for d in stuck:
                d.status = ds.FAILED
                d.error_message = "worker restarted before task could complete"
                d.completed_at = now
                d.retriable = True
                session.add(d)
            session.commit()
            return len(stuck)
    except Exception as exc:  # noqa: BLE001 — reaper must never block startup
        logger.error("Orphaned-download reaper failed: %s", exc)
        return 0


def _reap_orphaned_library_jobs() -> int:
    """Library job runs left 'running' by the previous worker process become
    failed (``worker_lost``) — same caveat as the download reaper above."""
    from sqlmodel import Session
    from db.engine import engine
    from services.library_jobs import reap_orphaned_running

    try:
        with Session(engine) as session:
            return reap_orphaned_running(session)
    except Exception as exc:  # noqa: BLE001 — reaper must never block startup
        logger.error("Orphaned library job reaper failed: %s", exc)
        return 0


@worker_ready.connect
def _on_worker_ready(**kwargs) -> None:
    """The worker's liveness signal, one per queue it consumes
    (``services/worker_health.py``): a queued library job is failed as
    ``worker_unavailable`` when no worker of the jobs queue is alive."""
    from services.worker_health import start_heartbeat
    start_heartbeat(sorted(_consumed))


def get_registry() -> ProviderRegistry:
    """The registry of the process running the task: built on its first use and kept up to
    date by its own reloader, never the copy of the parent that forked it."""
    return _process_registry.get()


def get_config() -> ConfigManager:
    if _config is None:
        raise RuntimeError("ConfigManager non inizializzato — worker non ancora avviato?")
    return _config
