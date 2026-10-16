"""
Riconciliazione dei file della libreria (``services.library_reconcile``): le
voci della libreria confrontate con i file che lo storage provider contiene
davvero. Senza ``--apply`` è una prova a secco: il database non cambia, il
resoconto dice cosa farebbe una riconciliazione vera.

Uso, dalla cartella ``app/``::

    python -m scripts.library_reconcile                 # prova a secco
    python -m scripts.library_reconcile --json out.json # resoconto completo
    python -m scripts.library_reconcile --apply         # applica

Lo stesso lavoro gira come routine ``reconcile_library_files``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from sqlmodel import Session

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import engine, init_db  # noqa: E402
from services.auth import auth_manager_for  # noqa: E402
from services.config_manager import get_config  # noqa: E402
from services.credentials import shared_store  # noqa: E402
from services.library_reconcile import DEFAULT_MAX_SIZE_CHECKS, reconcile_library_files  # noqa: E402
from services.log_redaction import install_log_redaction  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--json", type=Path, help="write the whole report (every example) to this file")
    parser.add_argument("--samples", type=int, default=25, help="examples of each outcome in the report")
    parser.add_argument("--max-size-checks", type=int, default=DEFAULT_MAX_SIZE_CHECKS,
                        help="sizes asked to the storage at most")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    install_log_redaction()
    init_db()
    with Session(engine) as session:
        report = reconcile_library_files(
            session, ProviderRegistry(), get_config(), auth_manager_for(shared_store(session)),
            apply=args.apply, max_size_checks=args.max_size_checks,
            samples=args.samples if not args.json else 1_000_000,
        )

    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False))
        print(f"Full report: {args.json}")
    return 0 if report.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
