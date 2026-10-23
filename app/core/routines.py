"""
Dichiarazione dei "system routines" (job periodici in background). Un
routine e' definito UNA VOLTA qui (chiave, task Celery, default, minimi):
da qui derivano sia le SettingDef di configurazione (``core/
settings_catalog.py``, ``GROUP_ROUTINES``) sia lo scheduler dinamico
(``workers/scheduler.py``). Aggiungere un routine è un'unica voce in
``ROUTINES``.

Modulo senza dipendenze pesanti (niente Celery/DB/ConfigManager), vive in
``core/`` proprio per questo: lo importano ``core/settings_catalog.py`` e
``workers/scheduler.py`` senza creare cicli — solo dati.

Provider-agnostico: ``task_name`` referenzia solo task interni.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RoutineDef:
    """Dichiarazione di un singolo routine periodico."""

    key: str  # slug stabile: usato nelle chiavi di setting e in routine_runs.routine_key
    task_name: str  # nome REGISTRATO del task Celery da eseguire (celery.tasks[task_name])
    default_interval_minutes: int
    min_interval_minutes: int = 1
    default_enabled: bool = True
    # Durata massima plausibile di UNA esecuzione: oltre questa una riga
    # 'running' e' considerata orfana (worker morto), non un'esecuzione
    # lenta ma sana — vedi db/routine_run_store.reap_stale_run.
    max_runtime_minutes: int = 60
    # The work the routine sets going and that outlives its run (tasks it
    # queued, chains it resumed): the name of a counter of the host
    # (``services/routines.ACTIVE_WORK``), shown next to its last run.
    active_work: str | None = None
    # The routine whose end queues this one (``workers.scheduler``): it runs
    # after each run of that routine, ok or failed, and the tick does not
    # start it while that routine waits or runs. On its own period otherwise.
    follows: str | None = None
    # The task reports its progress while it runs (phase, processed/total):
    # the run is handed a reporter (``services.routines.RunProgress``).
    reports_progress: bool = False
    # A job that is done once, not periodic work: the key of a SYSTEM setting that is True when it has
    # finished. From then on the tick never dispatches it by itself and the page shows it as completed;
    # "Run now" still runs it (a forced repeat). What sets the flag back to anything else re-arms it.
    done_when: str | None = None


def is_done(routine: RoutineDef, system_settings: Mapping[str, Any]) -> bool:
    """True when ``routine`` declares a completion flag and the flag is set."""
    return routine.done_when is not None and system_settings.get(routine.done_when) is True


# The system flag that says the library the storage already held was imported (services.library_scan).
LIBRARY_SCANNED = "library_scanned"

# The routine that brings the database in line with the sync providers'
# libraries; the library scan and the likes sync start it.
SYNC_LIBRARY_INDEX = "sync_library_index"

# The routine that mirrors the users' playlists on the sync providers; the
# edits of a playlist and the end of a followed playlist's refresh start it.
SYNC_PLAYLISTS = "sync_playlists"

ROUTINES: tuple[RoutineDef, ...] = (
    RoutineDef(
        # The audio files the storage provider already holds become the library, once per installation
        # (services.library_scan). It is a routine and not a step of the init container because the
        # providers start only after the API told the gateway which are installed: it waits, run after
        # run, until a storage provider answers, and does nothing at all once the library is imported.
        # Its result is "waiting" until then. A one-time job: once the flag is set the tick leaves it alone
        # and the page shows it as done; "Run now" scans again (new files only). Clearing the flag re-arms it.
        key="import_existing_library",
        task_name="workers.tasks.import_existing_library",
        default_interval_minutes=5,
        max_runtime_minutes=240,  # lists and reads the tags of the whole library
        done_when=LIBRARY_SCANNED,
    ),
    RoutineDef(
        key="sync_saved_tracks",
        task_name="workers.tasks.sync_all_users_saved_tracks",
        default_interval_minutes=6 * 60,
        min_interval_minutes=15,
        max_runtime_minutes=240,  # dispaccia un task per utente: puo' legittimamente durare
        active_work="library_sync_jobs",
    ),
    RoutineDef(
        key="sweep_delegations",
        task_name="workers.maintenance.sweep_delegations",
        default_interval_minutes=30,
        min_interval_minutes=5,
        active_work="delegated_downloads",
    ),
    RoutineDef(
        key="reconcile_pending_likes",
        task_name="workers.maintenance.reconcile_pending_likes",
        default_interval_minutes=60,
        min_interval_minutes=5,
        active_work="likes_to_propagate",
    ),
    RoutineDef(
        # Ogni file dei sync provider ha un record con il suo sync id, i sync
        # id che il provider non elenca più si tolgono (services.sync_index).
        # Ogni ora, come quando girava dentro l'abbinamento dei like: un
        # elenco di una libreria già indicizzata costa pochi secondi, e un
        # file aggiunto al server musicale fuori da Musizerr entra in
        # libreria entro un'ora. La scansione e la sincronizzazione dei like
        # lo avviano subito. Sulla coda jobs (TASK_ROUTES): la prima
        # indicizzazione di una libreria grande dura minuti.
        key=SYNC_LIBRARY_INDEX,
        task_name="workers.tasks.sync_library_index",
        default_interval_minutes=60,
        min_interval_minutes=15,
        reports_progress=True,
    ),
    RoutineDef(
        # I like importati dagli altri provider che corrispondono a tracce in
        # libreria diventano preferiti (services.favorites.match_likes). Gira
        # dopo ogni aggiornamento della libreria dal sync provider (anche
        # quello avviato da una scansione o da una sincronizzazione dei like).
        key="match_likes",
        task_name="workers.tasks.match_likes",
        default_interval_minutes=60,
        min_interval_minutes=5,
        follows=SYNC_LIBRARY_INDEX,
    ),
    RoutineDef(
        # Le playlist degli utenti (proprie e seguite) si copiano sul loro
        # account del sync provider che le tiene (services.playlist_sync).
        # Segue l'indicizzazione della libreria: una traccia appena scaricata
        # ha un id sul sync provider solo dopo l'indice. Una playlist
        # modificata o appena seguita lo avvia subito (start_routine).
        key=SYNC_PLAYLISTS,
        task_name="workers.tasks.sync_playlists",
        default_interval_minutes=60,
        min_interval_minutes=5,
        follows=SYNC_LIBRARY_INDEX,
        reports_progress=True,
    ),
    RoutineDef(
        # Le playlist seguite si confrontano con la loro sorgente (il
        # marcatore di versione del provider) e ne seguono le modifiche; le
        # tracce nuove si scaricano secondo la policy di approvazione
        # (services.playlist_monitor).
        key="refresh_followed_playlists",
        task_name="workers.tasks.refresh_followed_playlists",
        default_interval_minutes=60,
        min_interval_minutes=5,
        reports_progress=True,
        active_work="open_downloads",
    ),
    RoutineDef(
        key="library_duplicates_scan",
        task_name="workers.duplicates.scan_library_duplicates",
        default_interval_minutes=24 * 60,
        min_interval_minutes=60,
        default_enabled=False,  # richiede fingerprinting per candidato: mai attivo senza opt-in esplicito
        max_runtime_minutes=240,  # elenca+fingerprinta l'intera libreria: può legittimamente durare
    ),
    RoutineDef(
        # Le tracce il cui ultimo download è fallito per un errore
        # transitorio si rimettono in coda più tardi, poche per volta
        # (services.downloads.retry_transient_failures).
        key="retry_transient_failures",
        task_name="workers.tasks.retry_transient_failures",
        default_interval_minutes=60,
        min_interval_minutes=15,
        active_work="open_downloads",
    ),
    RoutineDef(
        # Le voci della libreria confrontate con i file che lo storage
        # contiene davvero: un file spostato si ritrova, uno sparito non
        # tiene più la traccia "in libreria" (services.library_reconcile).
        # Spento finché non lo si accende: la prima riconciliazione di una
        # libreria esistente va vista prima (scripts/library_reconcile.py).
        key="reconcile_library_files",
        task_name="workers.maintenance.reconcile_library_files",
        default_interval_minutes=24 * 60,
        min_interval_minutes=60,
        default_enabled=False,
        max_runtime_minutes=120,  # elenca l'intera libreria sullo storage
    ),
    RoutineDef(
        key="sweep_workspaces",
        task_name="workers.maintenance.sweep_workspaces",
        default_interval_minutes=60,
        min_interval_minutes=15,
    ),
    RoutineDef(
        # Reads the releases of Musizerr on GitHub (ADR-0007 §5.1): Administration says when a newer
        # version exists and what updating to it implies. Musizerr is updated by hand; this changes nothing.
        key="check_for_updates",
        task_name="workers.maintenance.check_for_updates",
        default_interval_minutes=24 * 60,
        min_interval_minutes=60,
    ),
)

_BY_KEY: dict[str, RoutineDef] = {r.key: r for r in ROUTINES}


def get_routine(key: str) -> RoutineDef | None:
    return _BY_KEY.get(key)


def enabled_setting_key(routine_key: str) -> str:
    return f"routine.{routine_key}.enabled"


def interval_setting_key(routine_key: str) -> str:
    return f"routine.{routine_key}.interval_minutes"
