"""
A user's library jobs — the import of their likes from the other providers
(``library_sync``) and the download of the likes not in the library
(``download_sync``) — as one state machine per user and kind:

    queued ──worker starts──▶ running ──▶ completed | failed
       └──────── stale ──────────┴──────▶ failed (worker_unavailable | worker_lost)

- ``start`` persists the run ``queued`` BEFORE enqueuing its task, and is
  idempotent: while a run is queued or running, it returns that run.
- The worker claims the run (``JobReporter.claim``), reports generic progress
  (a phase key, the provider it works on, processed/total, one entry per
  provider) with throttled, short writes, and finishes it.
- ``reap_stale`` closes the runs no worker will ever finish: queued while no
  worker is alive (``services.worker_health``) or for too long, running
  without a heartbeat for too long.
- Every progress write is also published on the progress stream as a
  ``library_job`` event for the run's owner only; the status routes stay the
  source of truth.

Provider-agnostic: providers are opaque names, phases are generic keys.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from core.utc import as_utc, utc_iso
from db import library_job_store as store
from db.library_job_store import (
    KIND_DOWNLOAD_SYNC,
    KIND_LIBRARY_SYNC,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from db.models import LibraryJobRun
from services.log_redaction import redact
from services.progress import publish_event
from services.worker_health import worker_alive
from workers.celery_app import QUEUE_JOBS, celery

logger = logging.getLogger(__name__)


# The registered name of each kind's worker task.
TASKS = {
    KIND_LIBRARY_SYNC: "workers.tasks.sync_saved_tracks_for_user",
    KIND_DOWNLOAD_SYNC: "workers.tasks.library_download_sync",
}

# Phase keys (generic; the UI names them).
PHASE_FETCHING = "fetching"  # reading the likes from a provider
PHASE_SAVING = "saving"  # storing them
PHASE_MATCHING = "matching"  # matching them with the library
PHASE_PREPARING = "preparing"  # download sync: reading the missing likes
PHASE_CHECKING = "checking"  # download sync: looking for each one, queuing the missing ones

EVENT_TYPE = "library_job"

# A queued run older than this, with no worker of the jobs queue alive, will
# not start.
QUEUED_NO_WORKER_SECONDS = 90
# A queued run older than this will not start even with a worker alive (its
# message was lost): the jobs queue holds every user's jobs one after the
# other (the periodic sync queues one per user at once), so it is long.
QUEUED_MAX_SECONDS = 2 * 60 * 60
# A running run without a heartbeat for this long lost its worker. Long
# enough for one slow provider call between two progress writes.
RUNNING_STALE_SECONDS = 10 * 60

# Run-level error codes, with their readable message.
WORKER_UNAVAILABLE = "worker_unavailable"
WORKER_LOST = "worker_lost"
QUEUE_UNAVAILABLE = "queue_unavailable"
ACCOUNT_REQUIRED = "account_required"
PROVIDER_NOT_CONNECTED = "provider_not_connected"
NO_LIBRARY_PROVIDER = "no_library_provider"
JOB_FAILED = "job_failed"

MESSAGES = {
    WORKER_UNAVAILABLE: "No worker is running to start the job.",
    WORKER_LOST: "The worker stopped before the job finished.",
    QUEUE_UNAVAILABLE: "The job could not be queued.",
    ACCOUNT_REQUIRED: "This job needs a signed-in account.",
    PROVIDER_NOT_CONNECTED: "Connect your account in Settings to sync your library.",
    NO_LIBRARY_PROVIDER: "No installed provider keeps a likes library to import.",
    JOB_FAILED: "The job failed.",
}


class QueueUnavailable(Exception):
    """The task could not be enqueued; the run was closed as failed."""

    def __init__(self, run: LibraryJobRun):
        super().__init__(MESSAGES[QUEUE_UNAVAILABLE])
        self.run = run


def _dispatch(task_name: str, kwargs: dict, task_id: str) -> None:
    """Queues the task by its registered name (the only way services reach
    the workers); the route table sends it to the jobs queue, which never
    waits behind downloads or routines (``workers/celery_app.py``)."""
    celery.send_task(task_name, kwargs=kwargs, task_id=task_id)


def start(
    session: Session, user_id: str, kind: str, params: Optional[dict] = None,
    *, dispatch: Optional[Callable[[str, dict, str], None]] = None,
) -> tuple[LibraryJobRun, bool]:
    """The user's active run of ``kind`` and False, or a new queued run (its
    task enqueued) and True. Raises ``QueueUnavailable`` if the task could not
    be enqueued."""
    reap_stale(session, user_id=user_id)
    existing = store.active_run(session, user_id, kind)
    if existing is not None:
        return existing, False
    task_id = str(uuid.uuid4())
    try:
        run = store.create_run(session, user_id, kind, task_id=task_id, params=params or {})
    except IntegrityError:
        # A concurrent request created it first: that is the run.
        session.rollback()
        existing = store.active_run(session, user_id, kind)
        if existing is None:
            raise
        return existing, False

    try:
        (dispatch or _dispatch)(TASKS[kind], {"user_id": user_id, "run_id": run.id, **(params or {})}, task_id)
    except Exception as exc:  # noqa: BLE001 — broker down: the run must not stay queued
        logger.warning("Could not enqueue %s for %s: %s", kind, user_id, exc)
        store.finish_run(session, run.id, status=STATUS_FAILED, error_code=QUEUE_UNAVAILABLE,
                         error_message=MESSAGES[QUEUE_UNAVAILABLE], error_detail=redact(str(exc)))
        session.refresh(run)
        raise QueueUnavailable(run) from exc
    store.prune_runs(session, user_id, kind)
    return run, True


def reap_stale(
    session: Session, *, user_id: Optional[str] = None, now: Optional[datetime] = None,
    alive: Optional[Callable[[], Optional[bool]]] = None,
) -> list[str]:
    """Closes as failed the active runs (of ``user_id``, or everyone's) that
    no worker will finish; returns their ids. The worker's liveness is asked
    only when a queued run is past the short grace."""
    now = now or datetime.now(timezone.utc)
    reaped: list[str] = []
    liveness: list[Optional[bool]] = []

    def is_alive() -> Optional[bool]:
        if not liveness:
            liveness.append(alive() if alive else worker_alive(QUEUE_JOBS))
        return liveness[0]

    for run in store.active_runs(session, user_id):
        if run.status == STATUS_QUEUED:
            age = (now - as_utc(run.created_at)).total_seconds()
            if age < QUEUED_NO_WORKER_SECONDS:
                continue
            if age < QUEUED_MAX_SECONDS and is_alive() is not False:
                continue
            code, expected = WORKER_UNAVAILABLE, (STATUS_QUEUED,)
        else:
            last = as_utc(run.heartbeat_at or run.started_at or run.created_at)
            if (now - last).total_seconds() < RUNNING_STALE_SECONDS:
                continue
            code, expected = WORKER_LOST, (STATUS_RUNNING,)
        if store.finish_run(session, run.id, status=STATUS_FAILED, expected=expected,
                            error_code=code, error_message=MESSAGES[code]):
            reaped.append(run.id)
            _publish(session, run.id)
    return reaped


def reap_orphaned_running(session: Session) -> int:
    """At worker start: a run left running by the previous worker process
    will never finish (its message was acknowledged by a dead process)."""
    count = 0
    for run in store.active_runs(session):
        if run.status == STATUS_RUNNING and store.finish_run(
            session, run.id, status=STATUS_FAILED, expected=(STATUS_RUNNING,),
            error_code=WORKER_LOST, error_message=MESSAGES[WORKER_LOST],
        ):
            count += 1
    return count


def public_run(run: LibraryJobRun, *, show_detail: bool) -> dict[str, Any]:
    """The run as the API and the progress stream show it. The raw error text
    (``error_detail``) is for admins only."""
    providers = []
    for entry in store.load(run.providers, []):
        item = {key: entry.get(key) for key in (
            "provider", "status", "processed", "total", "count", "error_code", "error_message",
        )}
        item["processed"] = item["processed"] or 0
        item["count"] = item["count"] or 0
        if show_detail and entry.get("error_detail"):
            item["error_detail"] = entry["error_detail"]
        providers.append(item)
    body: dict[str, Any] = {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "task_id": run.task_id,
        "params": store.load(run.params, {}),
        "phase": run.phase,
        "provider": run.provider,
        "processed": run.processed or 0,
        "total": run.total,
        "providers": providers,
        "result": store.load(run.result, {}),
        "queued_at": utc_iso(run.created_at),
        "started_at": utc_iso(run.started_at),
        "heartbeat_at": utc_iso(run.heartbeat_at),
        "completed_at": utc_iso(run.completed_at),
        "error_code": run.error_code,
        "error_message": run.error_message,
    }
    if show_detail and run.error_detail:
        body["error_detail"] = run.error_detail
    return body


def latest_public_run(session: Session, user_id: str, kind: str, *, show_detail: bool) -> dict[str, Any]:
    """The user's latest run of ``kind`` (stale ones closed first), or
    ``{"kind", "status": "never_run"}``."""
    reap_stale(session, user_id=user_id)
    run = store.latest_run(session, user_id, kind)
    if run is None:
        return {"kind": kind, "status": "never_run"}
    session.refresh(run)
    return public_run(run, show_detail=show_detail)


def _publish(session: Session, run_id: str) -> None:
    run = session.get(LibraryJobRun, run_id)
    if run is None:
        return
    session.refresh(run)
    try:
        publish_event({"type": EVENT_TYPE, "user_id": run.user_id, "run": public_run(run, show_detail=False)})
    except Exception as exc:  # noqa: BLE001 — the stream is a nicety, the row is the truth
        logger.debug("library job event not published: %s", exc)


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


def _default_session() -> Session:
    from db.engine import engine
    return Session(engine)


class JobReporter:
    """A worker's handle on its run: claims it, reports progress (throttled:
    a write when the phase or provider changes, or when ``min_interval``
    seconds or ``every_items`` items passed since the last one), finishes
    it. Every write is its own short transaction: call it only where the
    job holds no open write transaction (after a commit)."""

    def __init__(
        self, run_id: str, *, session_factory: Callable[[], Session] = _default_session,
        min_interval: float = 2.0, every_items: int = 250, clock: Callable[[], float] = time.monotonic,
    ):
        self.run_id = run_id
        self._session_factory = session_factory
        self._min_interval = min_interval
        self._every_items = every_items
        self._clock = clock
        self.providers: list[dict[str, Any]] = []
        self.phase: Optional[str] = None
        self.provider: Optional[str] = None
        self.processed = 0
        self.total: Optional[int] = None
        self.writes = 0
        # Set when a write finds the run no longer running (closed as stale):
        # the job should stop.
        self.cancelled = False
        self._last_key: tuple = ()
        self._last_at = float("-inf")
        self._last_processed = 0

    @classmethod
    def claim(
        cls, user_id: str, kind: str, run_id: Optional[str], *,
        session_factory: Callable[[], Session] = _default_session, **kwargs,
    ) -> Optional["JobReporter"]:
        """queued → running for ``run_id``; without one (a message queued
        before runs existed, or a direct call) a new running run. None if the
        run is not queued any more (closed as stale) or another run of the
        same kind is active: the task must not run."""
        with session_factory() as session:
            if run_id is None:
                try:
                    run_id = store.create_run(session, user_id, kind, status=STATUS_RUNNING).id
                except IntegrityError:
                    return None
            elif not store.mark_running(session, run_id):
                return None
            _publish(session, run_id)
        return cls(run_id, session_factory=session_factory, **kwargs)

    # -- providers ---------------------------------------------------------

    def set_providers(self, names: Iterable[str]) -> None:
        self.providers = [
            {"provider": name, "status": STATUS_QUEUED, "processed": 0, "total": None, "count": 0,
             "error_code": None, "error_message": None}
            for name in names
        ]

    def provider_entry(self, name: str) -> dict[str, Any]:
        for entry in self.providers:
            if entry["provider"] == name:
                return entry
        entry = {"provider": name, "status": STATUS_QUEUED, "processed": 0, "total": None, "count": 0,
                 "error_code": None, "error_message": None}
        self.providers.append(entry)
        return entry

    def update_provider(self, name: str, **fields: Any) -> None:
        """Changes one provider's entry; written with the next progress write."""
        self.provider_entry(name).update(fields)

    def fail_provider(self, name: str, code: str, message: str, detail: Optional[str] = None) -> None:
        self.update_provider(name, status=STATUS_FAILED, error_code=code, error_message=message,
                             error_detail=redact(detail) if detail else None)

    # -- progress ------------------------------------------------------------

    def progress(
        self, phase: str, *, provider: Optional[str] = None, processed: int = 0,
        total: Optional[int] = None, force: bool = False,
    ) -> bool:
        """Records the progress; writes it when due. True if it was written."""
        self.phase, self.provider, self.processed, self.total = phase, provider, processed, total
        if provider is not None:
            entry = self.provider_entry(provider)
            entry["status"] = STATUS_RUNNING if entry["status"] == STATUS_QUEUED else entry["status"]
            entry["processed"], entry["total"] = processed, total
        key = (phase, provider)
        now = self._clock()
        due = (
            force
            or key != self._last_key
            or (total is not None and processed >= total)
            or now - self._last_at >= self._min_interval
            or processed - self._last_processed >= self._every_items
        )
        if not due:
            return False
        self._write()
        self._last_key, self._last_at, self._last_processed = key, now, processed
        return True

    def flush(self) -> None:
        self._write()
        self._last_at = self._clock()

    def _write(self) -> None:
        with self._session_factory() as session:
            ok = store.save_progress(
                session, self.run_id, phase=self.phase, provider=self.provider,
                processed=self.processed, total=self.total, providers=self.providers,
            )
            if not ok:
                self.cancelled = True
                return
            self.writes += 1
            _publish(session, self.run_id)

    def finish(
        self, status: str, *, result: Optional[dict] = None, error_code: Optional[str] = None,
        error_message: Optional[str] = None, error_detail: Optional[str] = None,
    ) -> bool:
        with self._session_factory() as session:
            done = store.finish_run(
                session, self.run_id, status=status, expected=(STATUS_RUNNING,), result=result,
                providers=self.providers, error_code=error_code,
                error_message=error_message or (MESSAGES.get(error_code) if error_code else None),
                error_detail=redact(error_detail) if error_detail else None,
            )
            _publish(session, self.run_id)
        return done

