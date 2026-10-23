"""
Importazione della libreria esistente: indicizza ogni file audio che lo
storage provider configurato contiene già, una volta sola per installazione
(flag di sistema ``library_scanned``).

Gira come routine (``import_existing_library``, :func:`import_existing_library`)
e a mano (``scripts/library_scan.py``), mai all'avvio dell'init: i provider li
avvia il gateway solo dopo che l'API gli ha detto quali sono installati, quindi
un init che scansiona vede un registry vuoto. La routine riprova finché lo
storage non risponde.

Il provider elenca i propri file (``StorageProvider.list_files``) e rende
ognuno in una cartella di lavoro (``stream``), da cui l'host legge i tag;
l'identità della traccia è quella condivisa di ``db.track_store``.
"""
from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Iterable

from sqlmodel import Session, select

from musizerr_sdk.auth import AuthSession
from musizerr_sdk.interfaces import StorageProvider
from musizerr_sdk.models import Track
from core.routines import SYNC_LIBRARY_INDEX
from core.tagging import read_track_tags
from db.models import LibraryEntry
from db.settings_store import SettingsRepository
from db.track_store import find_or_create_track
from db.upsert import insert_ignore
from services.routines import start_routine
from services.storage import resolve_storage_provider
from services.workspace import open_workspace

logger = logging.getLogger(__name__)

LIBRARY_SCANNED = "library_scanned"
AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".m4a", ".opus", ".wav"})


def import_existing_library(session: Session, registry, config, auth_manager) -> dict:
    """The routine ``import_existing_library``: runs the one-time library scan if it is still to
    be done. The result says where it stands (``state``) and is a success in every case: a storage
    that is not there yet (no provider installed, not configured, not answering) is "waiting", and the
    routine looks again at its next run. ``detail`` is for the page when the result has no numbers."""
    if SettingsRepository(session).get_system(LIBRARY_SCANNED) is True:
        return {"status": "ok", "state": "already_imported", "detail": "The existing library was already imported."}
    indexed = scan_library_once(session, registry, config, auth_manager)
    if indexed is None:
        return {"status": "ok", "state": "waiting",
                "detail": "No storage provider answers yet: the library is imported once one does."}
    if SettingsRepository(session).get_system(LIBRARY_SCANNED) is True:
        return {"status": "ok", "state": "imported", "indexed": indexed}
    return {"status": "ok", "state": "empty", "detail": "The library on the storage is empty: looking again at the next run."}


def scan_library_once(session: Session, registry, config, auth_manager) -> int | None:
    """Indicizza la libreria dello storage provider configurato, se non è già
    stato fatto. Ritorna quanti file ha indicizzato, o None se la scansione
    non è avvenuta.

    Solo una scansione completa di una libreria con almeno un file audio la
    segna come scansionata. Senza storage provider, con uno che non sa
    elencare i propri file, se lo storage non risponde o se la libreria è
    vuota (un'installazione nuova, il cui storage non è ancora quello
    definitivo), il prossimo avvio riprova e salta i file già indicizzati."""
    settings = SettingsRepository(session)
    if settings.get_system(LIBRARY_SCANNED) is True:
        logger.info("Library already scanned, skipping.")
        return None

    storage, storage_name = resolve_storage_provider(registry, config.storage_provider)
    if storage is None:
        logger.warning("No storage provider available: the library scan waits for the next start.")
        return None

    try:
        auth = auth_manager.authenticate(storage)
        paths = storage.list_files("", auth)
        if paths is None:
            logger.warning(
                "Storage provider '%s' cannot list its files: the library scan waits for the next start.",
                storage_name,
            )
            return None
        logger.info("Scanning the library on storage provider '%s' ...", storage_name)
        found, indexed = index_library_files(paths, storage, storage_name, auth, config.workspace_dir, session)
    except Exception as exc:  # noqa: BLE001 — l'avvio non dipende dalla scansione
        session.rollback()
        logger.warning(
            "Library scan on '%s' stopped (%s): the next start resumes it.", storage_name, exc,
        )
        return None

    if not found:
        logger.info("The library on '%s' is empty: the next start looks again.", storage_name)
        return 0
    settings.set_system(LIBRARY_SCANNED, True)
    logger.info("Library scan complete: %d of %d audio files newly indexed.", indexed, found)
    if indexed:
        # Tracks just found may be liked on other providers: the library is
        # brought up to date from the sync providers, then the like matching
        # makes them favorites (``sync_library_index`` → ``match_likes``).
        start_routine(SYNC_LIBRARY_INDEX)
    return indexed


def index_library_files(
    paths: Iterable[str],
    storage: StorageProvider,
    storage_name: str,
    auth: AuthSession,
    workspace_root: Path,
    session: Session,
) -> tuple[int, int]:
    """Una ``LibraryEntry`` per ogni file audio di ``paths`` non ancora
    indicizzato, collegata alla traccia descritta dai suoi tag (nessuna se i
    tag non bastano a riconoscerla). Ritorna quanti file audio ha trovato e
    quanti ne ha indicizzati."""
    found = count = 0
    for relative in paths:
        if not is_library_audio(relative):
            continue
        found += 1
        if session.exec(select(LibraryEntry.id).where(LibraryEntry.file_path == relative)).first():
            continue

        track = _read_stored_track(storage, relative, auth, workspace_root)
        # A download that landed on the same path meanwhile keeps its entry.
        indexed = insert_ignore(session, LibraryEntry(
            track_id=find_or_create_track(track, session).internal_id if track else None,
            file_path=relative,
            file_format=PurePosixPath(relative).suffix.lstrip(".").lower(),
            source="scan",
            storage_provider=storage_name,
        ))
        session.commit()
        count += indexed
        if count % 100 == 0:
            logger.info("  ... %d files indexed so far", count)
    return found, count


def is_library_audio(relative: str) -> bool:
    """Un file audio della libreria: le cartelle nascoste (come quella di
    lavoro dell'host) non fanno parte della libreria."""
    path = PurePosixPath(relative)
    return path.suffix.lower() in AUDIO_EXTENSIONS and not any(
        part.startswith(".") for part in path.parts
    )


def _read_stored_track(
    storage: StorageProvider, relative: str, auth: AuthSession, workspace_root: Path,
) -> Track | None:
    with open_workspace(workspace_root, storage.name) as workspace:
        try:
            local = workspace.local_file(storage.stream(relative, str(workspace), auth))
        except Exception as exc:  # noqa: BLE001 — un file illeggibile non ferma la scansione
            logger.warning("Cannot read '%s' from storage: %s", relative, exc)
            return None
        return read_track_tags(local) if local else None
