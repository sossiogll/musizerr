"""
Vocabolario canonico degli stati di un download.

Unico e generico: ogni download provider (diretto o delegato) mappa il proprio
ciclo su questi stati. Nessun literal sparso nel codice, nessun nome di provider.
"""

from __future__ import annotations

from typing import Iterable

# The states a delegate can report (``DelegationStatus.state``) are part of the
# provider contract; the vocabulary below is built from those values and adds
# the two only the host assigns (waiting and retrying).
from musizerr_sdk.delegation import CONFLICT, DISPATCHED, DOWNLOADED, DOWNLOADING, FAILED, IMPORTING

PENDING = "pending"
RETRYING = "retrying"         # fallimento transitorio, ritento
# DOWNLOADING: recupero audio in corso (diretto o esterno)
# IMPORTING: atterraggio in libreria (store / import del delegato)
# CONFLICT: traccia già presente, da risolvere
# DISPATCHED: consegnato a un downloader esterno (ultimo stato deciso da musizerr)
# DOWNLOADED: terminale OK (ex "done")
# FAILED: terminale KO

# Marker of the error text of a download that could not be handed to the
# task queue (the broker was down): it failed before any worker saw it, for a
# transient reason (``retriable``), so the transient-failure routine queues it
# again. ``services/errors.py`` turns it into the error code of the same name.
QUEUE_UNAVAILABLE = "queue_unavailable"

# Ordine di "avanzamento" (usato per aggregare più tentativi: il più avanti vince).
ORDER: tuple[str, ...] = (
    DOWNLOADED, DISPATCHED, IMPORTING, DOWNLOADING, CONFLICT, RETRYING, PENDING, FAILED,
)

TERMINAL = frozenset({DOWNLOADED, FAILED})
# Richiesta ancora in corso: una nuova richiesta per la stessa traccia si aggancia.
OPEN = frozenset(ORDER) - TERMINAL
# Stati "in volo" gestiti dal worker: se il processo muore, vanno considerati orfani.
IN_FLIGHT = frozenset({DOWNLOADING, IMPORTING})

# Metadati per la UI data-driven (label + tono semaforico + fase). Niente
# mappe hardcoded lato UI: ``label_key`` è la chiave con cui la UI traduce
# (``label`` resta il testo di ripiego), ``phase`` raggruppa gli stati per ciò
# che l'utente può fare — ``queued`` (in attesa), ``active`` (in lavorazione),
# ``done`` (in libreria), ``failed`` (si può riprovare), ``review`` (serve una
# decisione).
PHASES: tuple[str, ...] = ("queued", "active", "done", "failed", "review")

LABELS: dict[str, dict[str, str]] = {
    PENDING: {"label": "In coda", "tone": "neutral", "phase": "queued"},
    DOWNLOADING: {"label": "Scaricamento", "tone": "info", "phase": "active"},
    IMPORTING: {"label": "Importazione", "tone": "info", "phase": "active"},
    CONFLICT: {"label": "Conflitto", "tone": "warning", "phase": "review"},
    RETRYING: {"label": "Nuovo tentativo", "tone": "warning", "phase": "queued"},
    DISPATCHED: {"label": "Delegato", "tone": "info", "phase": "active"},
    DOWNLOADED: {"label": "In libreria", "tone": "success", "phase": "done"},
    FAILED: {"label": "Fallito", "tone": "danger", "phase": "failed"},
}


def public_states() -> list[dict[str, str]]:
    """Lista ordinata degli stati con label/tono/fase per il frontend."""
    return [{"key": s, "label_key": f"download_states.{s}", **LABELS[s]} for s in ORDER]


_PRIORITY = {state: index for index, state in enumerate(ORDER)}


def best_status(statuses: Iterable[str]) -> str:
    """Lo stato più avanti nell'ordine canonico (``PENDING`` se non ce n'è
    nessuno): lo stato aggregato di più tentativi della stessa traccia."""
    return min(statuses, key=lambda status: _PRIORITY.get(status, len(ORDER)), default=PENDING)


def file_state(statuses: Iterable[str], in_library: bool) -> str | None:
    """Lo stato del FILE di una traccia, l'unico che la UI mostra: ``DOWNLOADED``
    se la traccia è in libreria (da qualunque fonte — un tentativo nuovo,
    anche forzato da un admin, non la toglie), altrimenti l'aggregato dei
    suoi tentativi; None se non è mai stata richiesta. Fuori dalla libreria un
    tentativo arrivato vale come fallito: il file in cui è arrivato non c'è
    più (lo storage l'ha perso), la traccia si può chiedere di nuovo."""
    if in_library:
        return DOWNLOADED
    attempts = [FAILED if status == DOWNLOADED else status for status in statuses]
    return best_status(attempts) if attempts else None


def phase_of(status: str | None) -> str | None:
    """La fase (``PHASES``) di uno stato, None per nessuno stato."""
    return LABELS[status]["phase"] if status in LABELS else None


def coverage(statuses: Iterable[str | None], total: int | None = None) -> dict[str, int]:
    """Quante tracce di una raccolta sono in libreria, in corso o fallite, dagli
    stati aggregati delle singole tracce (None = mai richiesta). ``total`` è
    il numero di tracce della raccolta, se è noto e maggiore di quelle viste."""
    counted = list(statuses)
    return {
        "total": max(total or 0, len(counted)),
        "downloaded": sum(1 for s in counted if s == DOWNLOADED),
        "in_progress": sum(1 for s in counted if s in OPEN),
        "failed": sum(1 for s in counted if s == FAILED),
    }


def song_coverage(songs: Iterable[tuple[str, str | None]], total: int | None) -> dict[str, int | None]:
    """THE library coverage of a release, shared by the album page and every
    discography or Home tile: ``songs`` are ``(song key, file state)`` pairs,
    one song counting once with its best state whatever the number of its
    copies (the same song in two editions); ``total`` the number of tracks
    of the release, ``None`` when no source knows it — then ``total`` stays
    ``None`` ("N in the library"), never the count of the songs seen, which
    would read as complete."""
    by_song: dict[str, list[str | None]] = {}
    for key, status in songs:
        by_song.setdefault(key, []).append(status)
    statuses = [best_status([s for s in states if s is not None]) if any(states) else None
                for states in by_song.values()]
    result: dict[str, int | None] = coverage(statuses, total=total)
    if total is None:
        result["total"] = None
    return result
