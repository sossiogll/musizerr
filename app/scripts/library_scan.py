"""
Indicizza ora la libreria che lo storage provider configurato contiene già
(``services.library_scan``); anche se è già stata importata una volta (i file
noti si saltano). È quello che fa
da sé la routine ``import_existing_library`` (ogni pochi minuti, finché lo
storage non risponde); qui lo si fa a mano e subito.

Va eseguito in un container che vede i socket dei provider (il volume
``provider_sockets``), per esempio
``docker compose exec app python -m scripts.library_scan``: senza, il registry
è vuoto e non c'è storage da scansionare. Non è il comando del container di
init (che applica solo le migrazioni: ``scripts/init_db.py``).

Uso: ``python -m scripts.library_scan`` dalla cartella ``app/``; gira anche
come ``python scripts/library_scan.py`` (il comando dei container di init
creati dalle versioni precedenti, che ``docker start`` riusa così com'è).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import engine
from services.auth import auth_manager_for
from services.config_manager import get_config
from services.credentials import shared_store
from services.database_init import init_db
from services.library_scan import scan_library_once
from services.log_redaction import install_log_redaction
from services.provider_registry import ProviderRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
install_log_redaction()


def main() -> None:
    init_db()
    with Session(engine) as session:
        scan_library_once(
            session, ProviderRegistry(), get_config(), auth_manager_for(shared_store(session)), force=True,
        )


if __name__ == "__main__":
    main()
