"""
Container di init: applica le migrazioni e, al primo avvio, indicizza la
libreria che lo storage provider configurato contiene già
(``services.library_scan``).

Uso: ``python -m scripts.library_scan`` dalla cartella ``app/``; gira anche
come ``python scripts/library_scan.py`` (il comando dei container di init
creati prima del passaggio a ``-m``, che ``docker start`` riusa così com'è).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import engine, init_db
from services.auth import auth_manager_for
from services.config_manager import get_config
from services.credentials import shared_store
from services.library_scan import scan_library_once
from services.log_redaction import install_log_redaction
from services.provider_registry import ProviderRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
install_log_redaction()


def main() -> None:
    init_db()
    with Session(engine) as session:
        scan_library_once(
            session, ProviderRegistry(), get_config(), auth_manager_for(shared_store(session)),
        )


if __name__ == "__main__":
    main()
