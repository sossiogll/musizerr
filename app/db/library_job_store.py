"""
Accesso alla tabella ``library_job_runs`` (vedi ``LibraryJobRun``). PURA
data-access: le regole (idempotenza, stale, progresso) vivono in
``services/library_jobs.py``.

Ogni scrittura è una transazione breve sulla propria sessione, e le
transizioni sono UPDATE condizionali sullo stato atteso: il worker non
riporta in vita una run che l'host ha già chiuso come stale, e una run
finita non torna indietro.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from .models import LibraryJobRun

KIND_LIBRARY_SYNC = "library_sync"
KIND_DOWNLOAD_SYNC = "download_sync"
KINDS = (KIND_LIBRARY_SYNC, KIND_DOWNLOAD_SYNC)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
ACTIVE = (STATUS_QUEUED, STATUS_RUNNING)

# Cronologia, non un log: le run finite più vecchie escono.
KEEP_RUNS_PER_KIND = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value)


def load(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def latest_run(session: Session, user_id: str, kind: str) -> Optional[LibraryJobRun]:
    return session.exec(
        select(LibraryJobRun)
        .where(LibraryJobRun.user_id == user_id, LibraryJobRun.kind == kind)
        .order_by(LibraryJobRun.created_at.desc(), LibraryJobRun.id.desc())
    ).first()


def active_run(session: Session, user_id: str, kind: str) -> Optional[LibraryJobRun]:
    return session.exec(
        select(LibraryJobRun).where(
            LibraryJobRun.user_id == user_id,
            LibraryJobRun.kind == kind,
            LibraryJobRun.status.in_(ACTIVE),
        )
    ).first()


def active_runs(session: Session, user_id: Optional[str] = None) -> list[LibraryJobRun]:
    query = select(LibraryJobRun).where(LibraryJobRun.status.in_(ACTIVE))
    if user_id is not None:
        query = query.where(LibraryJobRun.user_id == user_id)
    return list(session.exec(query).all())


def create_run(
    session: Session, user_id: str, kind: str, *, status: str = STATUS_QUEUED,
    task_id: Optional[str] = None, params: Optional[dict] = None,
) -> LibraryJobRun:
    """Inserisce e committa la run. Solleva ``IntegrityError`` se l'utente ne
    ha già una attiva dello stesso tipo (indice unico parziale)."""
    now = _now()
    run = LibraryJobRun(
        user_id=user_id, kind=kind, status=status, task_id=task_id, params=_dump(params),
        created_at=now,
        started_at=now if status == STATUS_RUNNING else None,
        heartbeat_at=now if status == STATUS_RUNNING else None,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _transition(session: Session, run_id: str, expected: tuple[str, ...], values: dict) -> bool:
    result = session.exec(
        update(LibraryJobRun)
        .where(LibraryJobRun.id == run_id, LibraryJobRun.status.in_(expected))
        .values(**values)
    )
    session.commit()
    return result.rowcount > 0


def mark_running(session: Session, run_id: str) -> bool:
    """queued → running. False se la run non è (più) in coda."""
    now = _now()
    return _transition(session, run_id, (STATUS_QUEUED,), {
        "status": STATUS_RUNNING, "started_at": now, "heartbeat_at": now,
    })


def save_progress(
    session: Session, run_id: str, *, phase: Optional[str], provider: Optional[str],
    processed: int, total: Optional[int], providers: Optional[list],
) -> bool:
    """Progresso e heartbeat di una run in corso. False se non è in corso."""
    return _transition(session, run_id, (STATUS_RUNNING,), {
        "phase": phase, "provider": provider, "processed": processed, "total": total,
        "providers": _dump(providers), "heartbeat_at": _now(),
    })


def finish_run(
    session: Session, run_id: str, *, status: str, expected: tuple[str, ...] = ACTIVE,
    result: Optional[dict] = None, providers: Optional[list] = None,
    error_code: Optional[str] = None, error_message: Optional[str] = None, error_detail: Optional[str] = None,
) -> bool:
    """Chiude una run attiva (``expected``). False se era già chiusa."""
    values: dict[str, Any] = {
        "status": status, "completed_at": _now(), "phase": None, "provider": None,
        "error_code": error_code, "error_message": error_message, "error_detail": error_detail,
    }
    if result is not None:
        values["result"] = _dump(result)
    if providers is not None:
        values["providers"] = _dump(providers)
    return _transition(session, run_id, expected, values)


def prune_runs(session: Session, user_id: str, kind: str, *, keep: int = KEEP_RUNS_PER_KIND) -> None:
    old = session.exec(
        select(LibraryJobRun)
        .where(LibraryJobRun.user_id == user_id, LibraryJobRun.kind == kind,
               LibraryJobRun.status.not_in(ACTIVE))
        .order_by(LibraryJobRun.created_at.desc(), LibraryJobRun.id.desc())
        .offset(keep)
    ).all()
    for run in old:
        session.delete(run)
    if old:
        session.commit()


def delete_finished(session: Session, user_id: str, kind: str) -> None:
    for run in session.exec(
        select(LibraryJobRun).where(
            LibraryJobRun.user_id == user_id, LibraryJobRun.kind == kind,
            LibraryJobRun.status.not_in(ACTIVE),
        )
    ).all():
        session.delete(run)
