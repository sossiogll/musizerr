"""
Puts a backup taken before a migration back (ADR-0007 §5, docs/migrations.md): the way back across a contract
migration, or after a migration that went wrong.

Without ``--apply`` it is a dry run: it checks that the file reads as a backup of this database's engine and says
what it would replace. With ``--apply`` it first takes a copy of the database as it is (``pre-restore-...``, in
the backups folder, never pruned), then replaces the content of the database with the backup:

- PostgreSQL: the ``public`` schema is dropped and created empty (the backup does not name the tables a later
  migration added), then ``pg_restore --single-transaction`` loads the backup: all or nothing, but if it fails
  the schema stays empty, so run it again (the backup is still there);
- SQLite: the backup API copies the backup into the live file.

STOP the application, the workers and beat first (``docker compose stop app worker worker-jobs
worker-maintenance beat``): a service connected to the database keeps writing, or blocks the restore. Then start the
version of Musizeer the backup was taken for. What was written after the backup is lost.

Usage, from ``app/`` (in the stack: ``docker compose run --rm init python -m scripts.restore_backup <file>``):

    python -m scripts.restore_backup /data/backups/premigration-...-from-X-to-Y.dump [--apply]

Exit status: 0 done (or a clean dry run), 1 the restore failed, 2 refused (nothing was touched).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import schema_guard  # noqa: E402
from db import migration_backup  # noqa: E402
from db.engine import POSTGRESQL, database_url  # noqa: E402
from services.database_init import backups_directory  # noqa: E402

logger = logging.getLogger("restore_backup")

SAFETY_PREFIX = "pre-restore"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup", type=Path, help="the backup file (premigration-...dump or ...sqlite3)")
    parser.add_argument("--apply", action="store_true", help="replace the database (default: dry run)")
    parser.add_argument("--skip-safety-copy", action="store_true",
                        help="do not copy the database as it is before replacing it (for a database that cannot be read)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    url = database_url()
    backend = url.get_backend_name()
    expected = migration_backup.EXTENSION.get(backend)
    path: Path = args.backup
    parsed = schema_guard.parse_backup_name(path.name)
    if expected is None or path.suffix.lstrip(".") != expected:
        logger.error("This database is %s and its backups end in .%s: %s is not one.", backend, expected, path.name)
        return 2
    try:
        migration_backup.check_backup(path, backend)
    except migration_backup.BackupError as exc:
        logger.error("%s", exc)
        return 2

    where = f"{url.host}/{url.database}" if backend == POSTGRESQL else str(url.database)
    if parsed is not None:
        logger.info("%s: taken at %s, before migrating from %s to %s.", path.name, parsed.stamp, parsed.source, parsed.target)
    logger.info("It reads as a backup. Restoring it would replace the content of %s.", where)
    if not args.apply:
        logger.info("Dry run: nothing was changed. Stop the application and the workers, then run again with --apply.")
        return 0

    if not args.skip_safety_copy:
        stamp = datetime.now(timezone.utc).strftime(schema_guard.STAMP_FORMAT)
        directory = backups_directory()
        try:
            copy = migration_backup.backup_database(
                url, directory, source="current", target="restore", now=datetime.now(timezone.utc),
            )
        except migration_backup.BackupError as exc:
            logger.error("The database as it is could not be copied first (%s). Fix that, or use --skip-safety-copy "
                         "if it cannot be read at all.", exc)
            return 2
        if copy is not None:
            kept = copy.with_name(f"{SAFETY_PREFIX}-{stamp}{copy.suffix}")
            copy.rename(kept)  # not a premigration-* name: the pruning of the real backups never takes it
            logger.info("The database as it was is in %s.", kept)
    try:
        migration_backup.restore_database(url, path)
    except migration_backup.BackupError as exc:
        logger.error("%s", exc)
        return 1
    logger.info("Restored. Start the version of Musizeer this backup was taken for%s.",
                f" (the schema before {parsed.target})" if parsed else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
