"""
Scheduler dinamico dei "system routines" (vedi ``core/routines.py``).

UN solo entry statico in beat, ``workers.routines.tick`` ogni 60s: legge le
impostazioni CORRENTI dalla tabella ``settings`` (mai la cache/lettura di
startup di un processo beat) e dispaccia i routine abilitati e scaduti, non
già in coda né in corso. Enabled/intervallo cambiano effetto entro un tick,
mai riavviando nulla — solo la cadenza del tick stesso resta statica.

Una run vive in ``routine_runs`` (``db/routine_run_store.py``):

- ``dispatch_routine`` crea la riga ``queued`` PRIMA di ``apply_async`` (mai
  dopo): se la riga comparisse solo all'avvio del task, un tick successivo
  lo crederebbe "non in corso" e lo dispaccerebbe di nuovo;
- ``run_routine`` la porta a ``running`` quando il worker la esegue davvero
  (``claim_run``) e la chiude con l'esito;
- un routine è dovuto quando sono passati ``interval`` minuti dal suo ultimo
  AVVIO REALE, non dal dispaccio: una run rimasta in coda non ne fa partire
  un'altra appena finita;
- una run pianificata parte solo se nessun'altra dello stesso routine è
  partita nel suo periodo (``period_minutes``): due messaggi per lo stesso
  periodo — una riga in coda data per persa e sostituita, due dispacci
  concorrenti — eseguono il routine una volta sola. "Esegui ora" (API) non
  ha periodo.

Sia il tick sia l'avvio manuale (API) passano da ``dispatch_routine``, e
così l'avvio da un evento dell'host (``workers.routines.start``, accodato per
nome da ``services.routines.start_routine``: una scansione, una
sincronizzazione dei like).

Una run gira sulla coda del task del routine (``TASK_ROUTES``, default
``maintenance``): l'indicizzazione della libreria del sync provider va su
``jobs``, così una libreria grande non tiene occupato il worker del tick.

Un routine che ne segue un altro (``RoutineDef.follows``) parte alla fine di
ogni run di quello, riuscita o fallita, se è abilitato; il tick non lo fa
partire mentre quello è in coda (con un worker vivo che lo può avviare) o in
corso. Così l'abbinamento dei like gira sempre dopo l'aggiornamento della
libreria, e da solo quando l'aggiornamento è spento.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from db.engine import engine
from db.models import RoutineRun
from db.routine_run_store import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RUNNING,
    active_run,
    claim_run,
    finish_run,
    last_started_at,
    prune_runs,
    queue_run,
    reap_stale_run,
)
from db.settings_store import SettingsRepository
from services.routines import RunProgress
from services.worker_health import worker_alive

from core.routines import ROUTINES, RoutineDef, enabled_setting_key, get_routine, interval_setting_key

from .celery_app import celery, queue_of

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh_routine_settings() -> dict:
    """Legge le impostazioni dei routine dalla tabella `settings` (mai dalla
    cache di ``ConfigManager`` o da una config di startup)."""
    with Session(engine) as session:
        return SettingsRepository(session).all_system()


def _due(routine_key: str, interval_minutes: int) -> bool:
    with Session(engine) as session:
        started = last_started_at(session, routine_key)
    return started is None or _now() - started >= timedelta(minutes=interval_minutes)


def dispatch_routine(routine: RoutineDef, *, period_minutes: int | None = None) -> RoutineRun | None:
    """Reap di un'eventuale riga stale, poi — se il routine non è già in
    coda né in corso — crea la riga 'queued' e SOLO DOPO accoda il task
    reale. None se già attivo (nessun dispaccio). ``period_minutes``: il
    periodo di una run pianificata (None per "Esegui ora"). Prende il
    ``RoutineDef`` già risolto dal chiamante (mai un secondo lookup per
    chiave qui)."""
    stale_after = routine.max_runtime_minutes * 60
    with Session(engine) as session:
        reap_stale_run(session, routine.key, stale_after_seconds=stale_after)
        if active_run(session, routine.key, stale_after_seconds=stale_after) is not None:
            return None
        run = queue_run(session, routine.key)

    run_routine.apply_async(
        kwargs={"routine_key": routine.key, "run_id": run.id, "period_minutes": period_minutes},
        queue=queue_of(routine.task_name),
    )
    return run


def _enabled(routine: RoutineDef, settings: dict) -> bool:
    return bool(settings.get(enabled_setting_key(routine.key), routine.default_enabled))


def _waits_for_leader(routine: RoutineDef) -> bool:
    """True while the routine it follows is running, or queued with a worker
    of its queue alive to start it: that run's end starts this one. A run
    queued with no worker to take it does not hold back the follower."""
    leader = get_routine(routine.follows) if routine.follows else None
    if leader is None:
        return False
    with Session(engine) as session:
        run = active_run(session, leader.key, stale_after_seconds=leader.max_runtime_minutes * 60)
    if run is None:
        return False
    return run.status == STATUS_RUNNING or worker_alive(queue_of(leader.task_name)) is not False


def _start_followers(routine_key: str) -> list[str]:
    """Dispatches the enabled routines that follow ``routine_key``."""
    followers = [r for r in ROUTINES if r.follows == routine_key]
    if not followers:
        return []
    settings = _fresh_routine_settings()
    started = []
    for follower in followers:
        if _enabled(follower, settings) and dispatch_routine(follower) is not None:
            started.append(follower.key)
    return started


@celery.task(name="workers.routines.tick")
def tick() -> dict:
    """Beat entry unico: dispaccia ogni routine abilitato e scaduto."""
    settings = _fresh_routine_settings()
    dispatched: list[str] = []

    # The routines another one follows first: a leader dispatched in this
    # tick already holds back its follower.
    for routine in sorted(ROUTINES, key=lambda r: r.follows is not None):
        if not _enabled(routine, settings):
            continue
        interval_minutes = int(settings.get(interval_setting_key(routine.key), routine.default_interval_minutes))
        if not _due(routine.key, interval_minutes):
            continue
        if _waits_for_leader(routine):
            continue
        if dispatch_routine(routine, period_minutes=interval_minutes) is not None:
            dispatched.append(routine.key)

    return {"dispatched": dispatched}


@celery.task(name="workers.routines.start")
def start(routine_key: str) -> dict:
    """Dispatches a routine now, outside its schedule (an event of the host:
    ``services.routines.start_routine``). Nothing if it is already queued or
    running: that run serves."""
    routine = get_routine(routine_key)
    if routine is None:
        logger.warning("start: routine sconosciuto '%s'", routine_key)
        return {"status": "error", "detail": f"unknown routine '{routine_key}'"}
    run = dispatch_routine(routine)
    return {"status": "dispatched" if run is not None else "already_active"}


@celery.task(name="workers.routines.run")
def run_routine(routine_key: str, run_id: str, period_minutes: int | None = None) -> dict:
    """Esegue il task reale del routine per NOME (``celery.tasks[...]``,
    mai un import specifico) e registra l'esito sulla riga già creata da
    ``dispatch_routine``. Non esegue nulla se la riga non è più in coda
    (chiusa come persa) o se il periodo è già coperto da un'altra run."""
    routine = get_routine(routine_key)
    if routine is None:
        logger.warning("run_routine: routine sconosciuto '%s'", routine_key)
        return {"status": "error", "detail": f"unknown routine '{routine_key}'"}

    period = period_minutes * 60 if period_minutes else None
    with Session(engine) as session:
        if claim_run(session, run_id, period_seconds=period) is None:
            logger.info("Routine '%s': run %s not started (no longer queued, or its period already ran)",
                        routine_key, run_id)
            return {"status": "skipped"}

    status = STATUS_OK
    detail = ""
    try:
        task = celery.tasks[routine.task_name]
        result = task.run(progress=RunProgress(run_id)) if routine.reports_progress else task.run()
        # A task that returns its failure as data ({"status": "error", ...})
        # failed: the run says so, never "ok" with the error inside.
        if isinstance(result, dict) and result.get("status") == STATUS_ERROR:
            status = STATUS_ERROR
        detail = _summarize(result)
    except Exception as exc:  # noqa: BLE001 — un routine che solleva non deve far sparire la run in corso
        status = STATUS_ERROR
        detail = str(exc)[:500]
        logger.warning("Routine '%s' fallito: %s", routine_key, exc)

    with Session(engine) as session:
        finish_run(session, run_id, status=status, detail=detail)
        prune_runs(session, routine_key)

    followers = _start_followers(routine_key)
    return {"status": status, "detail": detail, "followers": followers}


# A result longer than this is not kept whole (it is a summary, not a log).
_MAX_DETAIL = 2000


def _summarize(result) -> str:
    """The result of the task as JSON, the page renders it per routine
    (``services/routines.run_result``); plain text when it cannot be."""
    if result is None:
        return ""
    try:
        text = json.dumps(result, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(result)
    return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL]
