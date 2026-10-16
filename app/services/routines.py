"""
Host side of the system routines: the result of a run as data, how long it
took, its progress while it runs (``RunProgress``), the work a routine has
set going that outlives its run (``RoutineDef.active_work``) — read by the
routines page (``routes/system/routines.py``) — and the start of a routine
outside its schedule by an event of the host (``start_routine``).
"""
from __future__ import annotations

import ast
import json
import logging
import time
from typing import Any, Callable, Optional

from sqlmodel import Session, func, select

from core import download_states as ds
from core.utc import as_utc
from db import routine_run_store
from db.download_store import open_clause
from db.library_job_store import ACTIVE, KIND_LIBRARY_SYNC
from db.models import Download, LibraryJobRun, PendingLike, RoutineRun
from services.errors import public_error
from workers.celery_app import celery

logger = logging.getLogger(__name__)

# The registered task that dispatches a routine (``workers.scheduler``).
START_TASK = "workers.routines.start"


def start_routine(routine_key: str) -> bool:
    """Starts a routine now, as "Run now" does (its followers run after it):
    the event of the host that makes its work due (a library scan, a likes
    sync). Queued by name; the dispatch creates the run row. Nothing happens
    while the routine is already queued or running. False when it could not
    be queued (the routine runs on its schedule anyway)."""
    try:
        celery.send_task(START_TASK, kwargs={"routine_key": routine_key})
    except Exception as exc:  # noqa: BLE001 — the broker is down: the schedule catches up
        logger.warning("Routine %s not started: %s", routine_key, exc)
        return False
    return True


def _count(session: Session, query) -> int:
    return int(session.exec(query).one())


# The counters a routine may name in ``active_work``.
ACTIVE_WORK: dict[str, Callable[[Session], int]] = {
    # A likes sync queued or running for a user.
    "library_sync_jobs": lambda session: _count(session, select(func.count()).select_from(LibraryJobRun).where(
        LibraryJobRun.kind == KIND_LIBRARY_SYNC, LibraryJobRun.status.in_(ACTIVE))),
    # A download handed to an external system, followed by its reconciliation chain.
    "delegated_downloads": lambda session: _count(session, select(func.count()).select_from(Download).where(
        Download.status == ds.DISPATCHED, Download.delegation_handle.is_not(None))),
    # A favorite still to be propagated to the sync provider.
    "likes_to_propagate": lambda session: _count(session, select(func.count()).select_from(PendingLike).where(
        PendingLike.sync_provider.is_not(None))),
    # A download request queued or in progress.
    "open_downloads": lambda session: _count(session, select(func.count()).select_from(Download).where(open_clause())),
}


def active_work(session: Session, counter: str | None) -> int | None:
    """How much work of that kind is going on now; None without a counter."""
    count = ACTIVE_WORK.get(counter) if counter else None
    return count(session) if count else None


def run_result(detail: str | None) -> dict[str, Any] | None:
    """The result of a successful run as an object: stored as JSON, or as
    the Python repr of a dict by the runs written before (read with
    ``ast.literal_eval``, literals only). None when it is neither."""
    if not detail:
        return None
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(detail)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        return value if isinstance(value, dict) else None
    return None


def run_error(run: RoutineRun) -> Optional[dict[str, Any]]:
    """What a failed run says about its failure, as the task recorded it in
    its result (``"status": "error"``): ``{error_code, error_message,
    error_detail}`` as named there, or the classified text of its
    ``detail``. None when the run has no such result (an exception's text,
    classified by the caller)."""
    result = run_result(run.detail)
    if not result:
        return None
    if result.get("error_code"):
        return {key: result.get(key) for key in ("error_code", "error_message", "error_detail")}
    if isinstance(result.get("detail"), str):
        return public_error(result["detail"], "routine", show_detail=True)
    return None


def run_progress(run: RoutineRun) -> Optional[dict[str, Any]]:
    """The last progress a running run reported, with its time; None when
    it reported none."""
    if run.progress is None:
        return None
    try:
        progress = json.loads(run.progress)
    except ValueError:
        return None
    if not isinstance(progress, dict):
        return None
    return {**progress, "heartbeat_at": as_utc(run.heartbeat_at)}


def _default_session() -> Session:
    from db.engine import engine
    return Session(engine)


class RunProgress:
    """The progress of a routine run, reported by its task: the same
    conventions as a library job (``services.library_jobs.JobReporter``) —
    a phase key, the provider it works on, processed/total, and a heartbeat —
    throttled: a write when the phase or provider changes, when the total is
    reached, or when ``min_interval`` seconds or ``every_items`` items passed
    since the last one. Every write is its own short transaction: call it
    only where the task holds no open write transaction (after a commit)."""

    def __init__(
        self, run_id: str, *, session_factory: Callable[[], Session] = _default_session,
        min_interval: float = 2.0, every_items: int = 250, clock: Callable[[], float] = time.monotonic,
    ):
        self.run_id = run_id
        self._session_factory = session_factory
        self._min_interval = min_interval
        self._every_items = every_items
        self._clock = clock
        self.writes = 0
        # Set when a write finds the run no longer running (closed as stale).
        self.cancelled = False
        self._last_key: tuple = ()
        self._last_at = float("-inf")
        self._last_processed = 0

    def __call__(
        self, phase: str, *, provider: Optional[str] = None, processed: int = 0,
        total: Optional[int] = None, force: bool = False,
    ) -> bool:
        """Records the progress; writes it when due. True if it was written."""
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
        body = json.dumps({"phase": phase, "provider": provider, "processed": processed, "total": total})
        with self._session_factory() as session:
            if not routine_run_store.save_progress(session, self.run_id, body):
                self.cancelled = True
                return False
        self.writes += 1
        self._last_key, self._last_at, self._last_processed = key, now, processed
        return True


def run_duration_seconds(run: RoutineRun) -> float | None:
    """How long the run executed (its wait in the queue excluded)."""
    if run.finished_at is None or run.started_at is None:
        return None
    return round((as_utc(run.finished_at) - as_utc(run.started_at)).total_seconds(), 3)


def run_wait_seconds(run: RoutineRun) -> float | None:
    """How long the run waited in the queue before a worker started it."""
    if run.started_at is None:
        return None
    return round((as_utc(run.started_at) - as_utc(run.queued_at)).total_seconds(), 3)
