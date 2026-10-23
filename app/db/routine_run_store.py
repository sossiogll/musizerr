"""
Accesso alla tabella ``routine_runs`` (cronologia di esecuzione dei "system
routines", vedi ``core/routines.py``). PURA data-access: ``routine_key``
è una stringa opaca, nessun routine concreto è nominato qui.

Una run nasce ``queued`` al dispaccio (``queue_run``), diventa ``running``
quando il worker la prende davvero (``claim_run``, un UPDATE condizionale) e
si chiude ``ok`` / ``error`` / ``skipped``. Le transizioni sono condizionali
sullo stato atteso: il messaggio di una run già chiusa (data per persa e
sostituita) non la riporta in vita e non esegue nulla.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import exists, func, update
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from core.utc import as_utc

from .locks import xact_lock
from .models import RoutineRun

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
ACTIVE = (STATUS_QUEUED, STATUS_RUNNING)

# Default se il chiamante non passa lo stale-timeout del routine (vedi
# RoutineDef.max_runtime_minutes) — oltre questa età una riga attiva non è
# più creduta: il worker che l'aveva avviata è quasi certamente morto, o il
# messaggio di una run in coda è andato perso.
STALE_RUN_TIMEOUT_SECONDS = 3600.0

# Cronologia diagnostica, non un log da conservare per sempre.
KEEP_RUNS_PER_ROUTINE = 50

STALE_DETAIL = "stale: worker restarted or crashed mid-run"
LOST_DETAIL = "stale: never started (queue message lost)"
SKIPPED_DETAIL = "already ran in this period"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def queue_run(session: Session, routine_key: str) -> RoutineRun:
    """La run di un dispaccio, ``queued``: il task non è ancora partito."""
    run = RoutineRun(routine_key=routine_key, status=STATUS_QUEUED, queued_at=_now())
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def claim_run(session: Session, run_id: str, *, period_seconds: Optional[float] = None) -> Optional[RoutineRun]:
    """queued → running, ora: il worker la esegue. Con ``period_seconds`` (una
    run pianificata, non "Esegui ora") non parte se un'altra run dello
    stesso routine è partita meno di un periodo fa: allora si chiude
    ``skipped``. None se la run non va eseguita (non è più in coda, o il suo
    periodo è già coperto). Un solo UPDATE: due messaggi che arrivano
    insieme non partono entrambi (quelli con un periodo, uno alla volta per
    routine: ``xact_lock``)."""
    run = session.get(RoutineRun, run_id)
    if run is None:
        return None
    if period_seconds is not None:
        # The period check looks at the other runs of the routine: two runs
        # of it claiming together must not both pass it.
        xact_lock(session, f"routine-claim:{run.routine_key}")
    now = _now()
    conditions = [RoutineRun.id == run_id, RoutineRun.status == STATUS_QUEUED]
    if period_seconds is not None:
        other = aliased(RoutineRun)
        conditions.append(~exists().where(
            other.routine_key == run.routine_key,
            other.id != run_id,
            other.started_at > now - timedelta(seconds=period_seconds),
        ))
    claimed = session.exec(
        update(RoutineRun).where(*conditions).values(status=STATUS_RUNNING, started_at=now)
    ).rowcount > 0
    if not claimed:
        # Ancora in coda vuol dire che il periodo era già coperto.
        session.exec(
            update(RoutineRun)
            .where(RoutineRun.id == run_id, RoutineRun.status == STATUS_QUEUED)
            .values(status=STATUS_SKIPPED, detail=SKIPPED_DETAIL, finished_at=now)
        )
    session.commit()
    session.refresh(run)
    return run if claimed else None


def finish_run(session: Session, run_id: str, *, status: str, detail: str | None = None) -> bool:
    """Chiude una run in corso. False se non lo era più (chiusa come stale)."""
    result = session.exec(
        update(RoutineRun)
        .where(RoutineRun.id == run_id, RoutineRun.status == STATUS_RUNNING)
        .values(status=status, detail=detail, finished_at=_now())
    )
    session.commit()
    return result.rowcount > 0


def save_progress(session: Session, run_id: str, progress: str) -> bool:
    """Il progresso (JSON) e l'heartbeat di una run in corso. False se non
    lo è più (chiusa come stale): chi la esegue può fermarsi."""
    result = session.exec(
        update(RoutineRun)
        .where(RoutineRun.id == run_id, RoutineRun.status == STATUS_RUNNING)
        .values(progress=progress, heartbeat_at=_now())
    )
    session.commit()
    return result.rowcount > 0


def latest_run(session: Session, routine_key: str) -> Optional[RoutineRun]:
    return session.exec(
        select(RoutineRun)
        .where(RoutineRun.routine_key == routine_key)
        .order_by(RoutineRun.queued_at.desc())
    ).first()


def last_finished_run(
    session: Session, routine_key: str, statuses: tuple[str, ...] = (STATUS_OK, STATUS_ERROR),
) -> Optional[RoutineRun]:
    """L'ultima run conclusa ``ok`` o ``error`` (mai una saltata); ``statuses`` ne sceglie uno solo
    (l'ultima riuscita: ``(STATUS_OK,)``)."""
    return session.exec(
        select(RoutineRun)
        .where(RoutineRun.routine_key == routine_key, RoutineRun.status.in_(statuses),
               RoutineRun.finished_at.is_not(None))
        .order_by(RoutineRun.finished_at.desc())
    ).first()


def finished_runs(
    session: Session, routine_key: str, statuses: tuple[str, ...] = (STATUS_OK, STATUS_ERROR), limit: int = 10,
) -> list[RoutineRun]:
    """Le ultime ``limit`` run concluse con uno degli ``statuses``, la più recente per prima."""
    return list(session.exec(
        select(RoutineRun)
        .where(RoutineRun.routine_key == routine_key, RoutineRun.status.in_(statuses),
               RoutineRun.finished_at.is_not(None))
        .order_by(RoutineRun.finished_at.desc())
        .limit(limit)
    ).all())


def last_started_at(session: Session, routine_key: str) -> Optional[datetime]:
    """L'avvio reale più recente del routine (None se non è mai partito)."""
    return as_utc(session.exec(
        select(func.max(RoutineRun.started_at)).where(RoutineRun.routine_key == routine_key)
    ).one())


def _stale(run: RoutineRun, stale_after_seconds: float, now: datetime) -> bool:
    since = run.started_at if run.status == STATUS_RUNNING else run.queued_at
    return (now - as_utc(since)).total_seconds() >= stale_after_seconds


def active_run(
    session: Session, routine_key: str, *, stale_after_seconds: float = STALE_RUN_TIMEOUT_SECONDS,
) -> Optional[RoutineRun]:
    """L'ultima run se è in coda o in corso e non stale. Sola lettura — la
    chiusura di una riga stale è compito di ``reap_stale_run``."""
    run = latest_run(session, routine_key)
    if run is None or run.status not in ACTIVE or _stale(run, stale_after_seconds, _now()):
        return None
    return run


def reap_stale_run(session: Session, routine_key: str, *, stale_after_seconds: float = STALE_RUN_TIMEOUT_SECONDS) -> None:
    """Chiude come 'error' una riga attiva più vecchia dello stale-timeout:
    in corso da troppo (worker morto a metà) o in coda da troppo (messaggio
    perso). Chiamato PRIMA di ogni dispaccio, mai da una lettura. Se il
    messaggio di una run chiusa così arriva dopo, ``claim_run`` non la esegue."""
    run = latest_run(session, routine_key)
    now = _now()
    if run is None or run.status not in ACTIVE or not _stale(run, stale_after_seconds, now):
        return
    session.exec(
        update(RoutineRun)
        .where(RoutineRun.id == run.id, RoutineRun.status == run.status)
        .values(status=STATUS_ERROR, finished_at=now,
                detail=STALE_DETAIL if run.status == STATUS_RUNNING else LOST_DETAIL)
    )
    session.commit()


def prune_runs(session: Session, routine_key: str, *, keep: int = KEEP_RUNS_PER_ROUTINE) -> None:
    rows = session.exec(
        select(RoutineRun)
        .where(RoutineRun.routine_key == routine_key)
        .order_by(RoutineRun.queued_at.desc())
    ).all()
    if len(rows) <= keep:
        return
    for row in rows[keep:]:
        session.delete(row)
    session.commit()
