"""
API admin per i "system routines" (vedi core/routines.py e
workers/scheduler.py): stato, PATCH enabled/intervallo via
``SettingsService`` (mai un write diretto), avvio manuale via
``dispatch_routine`` (stesso lock anti-doppio-dispaccio del tick).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.routines import ROUTINES, enabled_setting_key, get_routine, interval_setting_key, is_done
from db.settings_store import SettingsRepository
from db.routine_run_store import STATUS_ERROR, STATUS_QUEUED, STATUS_RUNNING, active_run, last_started_at, latest_run
from routes.deps import AdminUserDep, ConfigDep, RegistryDep, SessionDep
from services import routines as routine_runs
from services.errors import public_error
from services.settings import SettingsService, SettingsWriteError
from workers.scheduler import dispatch_routine

router = APIRouter()


class RoutinePatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


# Beat dispaccia i routine dovuti a ogni tick (60s): oltre questo margine una
# esecuzione dovuta e non partita vuol dire che worker o beat non girano.
SCHEDULER_GRACE = timedelta(minutes=5)


def is_scheduler_stale(enabled: bool, next_run_at: datetime | None, now: datetime) -> bool:
    """True se un routine attivo aveva una prossima esecuzione più vecchia del
    margine: lo scheduler non lo sta facendo partire. Un routine disattivato o
    mai eseguito non dice nulla sullo scheduler: False."""
    if not enabled or next_run_at is None:
        return False
    return now - next_run_at > SCHEDULER_GRACE


@router.get("/system/routines")
def list_routines(admin: AdminUserDep, config: ConfigDep, registry: RegistryDep, session: SessionDep):
    """Stato di ogni routine: config corrente, ultima run (in coda dal
    dispaccio, avviata davvero, finita; esito come dato, attesa in coda e
    durata), in coda o in corso o no, lavoro avviato ancora in corso,
    prossima esecuzione stimata dall'ultimo avvio reale (None se mai
    partito, cioè già dovuto, o se è un lavoro una tantum già finito: ``done``) e ``scheduler_stale`` quando quella stima è
    scaduta da oltre il margine. ``scheduler_stale`` in cima vale True se lo
    è almeno un routine."""
    now = datetime.now(timezone.utc)
    service = SettingsService(session, config, registry)
    values = service.effective_values(admin, include_providers=False)

    system_flags = SettingsRepository(session).all_system()
    routines = []
    for routine in ROUTINES:
        done = is_done(routine, system_flags)
        enabled = bool(values.get(enabled_setting_key(routine.key), routine.default_enabled))
        interval_minutes = int(values.get(interval_setting_key(routine.key), routine.default_interval_minutes))

        run = latest_run(session, routine.key)
        last_run = None
        if run is not None:
            # A run's result is data (``result``), rendered by the UI per
            # routine; ``detail`` keeps only a text that is not. A failed run
            # keeps its result only when the task recorded its failure in it
            # (an error code, e.g. one per sync provider).
            error = routine_runs.run_error(run) if run.status == STATUS_ERROR else None
            result = routine_runs.run_result(run.detail) if run.status != STATUS_ERROR or error else None
            last_run = {
                "queued_at": run.queued_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "wait_seconds": routine_runs.run_wait_seconds(run),
                "duration_seconds": routine_runs.run_duration_seconds(run),
                "status": run.status,
                "result": result,
                "detail": None if result is not None else run.detail,
                "error_code": None,
                # Where a running run is (phase, provider, processed/total,
                # heartbeat_at); None for a routine that does not report.
                "progress": routine_runs.run_progress(run) if run.status == STATUS_RUNNING else None,
            }
            if error is not None:
                last_run.update(error)
            elif run.status == STATUS_ERROR and run.detail:
                last_run.update(public_error(run.detail, "routine", show_detail=True))
        started = last_started_at(session, routine.key)
        # A one-time job that is done has no next run (its tick skips it); "Run now" still works.
        next_run_at = None if done else (started + timedelta(minutes=interval_minutes) if started else None)
        active = active_run(session, routine.key, stale_after_seconds=routine.max_runtime_minutes * 60)

        routines.append({
            "key": routine.key,
            "task": routine.task_name,
            # The routine whose end starts this one (None: only its period).
            "follows": routine.follows,
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "min_interval_minutes": routine.min_interval_minutes,
            "last_run": last_run,
            "next_run_at": next_run_at,
            # A one-time job (``RoutineDef.done_when``) that has finished: not scheduled again by itself.
            "done": done,
            # Waiting in the queue / executing now (never both).
            "queued": active is not None and active.status == STATUS_QUEUED,
            "running": active is not None and active.status == STATUS_RUNNING,
            "scheduler_stale": is_scheduler_stale(enabled, next_run_at, now),
            # The work it set going that is still in progress (queued tasks,
            # live chains); None when the routine leaves none behind.
            "active_work": routine_runs.active_work(session, routine.active_work),
        })

    return {
        "routines": routines,
        "scheduler_stale": any(r["scheduler_stale"] for r in routines),
    }


@router.patch("/system/routines/{key}")
def patch_routine(
    key: str, body: RoutinePatch, admin: AdminUserDep, config: ConfigDep, registry: RegistryDep, session: SessionDep,
):
    """Aggiorna enabled/intervallo — instradato via ``SettingsService.write_values``
    (stessa validazione/permessi di ``PATCH /settings``, mai un write diretto)."""
    routine = get_routine(key)
    if routine is None:
        raise HTTPException(status_code=404, detail=f"unknown routine '{key}'")

    updates: dict = {}
    if body.enabled is not None:
        updates[enabled_setting_key(key)] = body.enabled
    if body.interval_minutes is not None:
        updates[interval_setting_key(key)] = body.interval_minutes
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    service = SettingsService(session, config, registry)
    try:
        service.write_values(updates, admin, include_providers=False)
    except SettingsWriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {"status": "ok"}


@router.post("/system/routines/{key}/run")
def run_routine_now(key: str, admin: AdminUserDep):  # noqa: ARG001
    """Avvia subito un routine, a meno che non sia già in coda o in corso
    (409) — stesso ``dispatch_routine`` del tick, stesso anti-doppio-dispaccio,
    senza il vincolo del periodo."""
    routine = get_routine(key)
    if routine is None:
        raise HTTPException(status_code=404, detail=f"unknown routine '{key}'")

    run = dispatch_routine(routine)
    if run is None:
        raise HTTPException(status_code=409, detail=f"routine '{key}' is already queued or running")

    return {"run_id": run.id}
