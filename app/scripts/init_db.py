"""
Container di init: applica le migrazioni del database (``db.engine.init_db``,
Alembic) ed esce. L'API, i worker e beat partono solo dopo che è finito con
successo (``depends_on: init: condition: service_completed_successfully``).

Non scansiona la libreria: i provider li avvia il gateway solo dopo che l'API
gli ha detto quali sono installati, quindi un init che scansiona vede un
registry vuoto. L'importazione della libreria esistente è la routine
``import_existing_library`` (``services.library_scan``), che aspetta lo storage.

Uso: ``python -m scripts.init_db`` dalla cartella ``app/``.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import environment
from services.database_init import SchemaError, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> int:
    log = logging.getLogger(__name__)
    environment.report_at_start()
    try:
        init_db()
    except SchemaError as refused:
        # The message says what to do. Exit status 1: the services behind `init` do not start.
        log.error("%s", refused)
        return 1
    log.info("Database is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
