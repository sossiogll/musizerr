"""
Bringing the database to the schema of this code, and refusing to run on one it cannot use (ADR-0007 §4).

- :func:`init_db` is the ``init`` step (``python -m scripts.init_db``) and what a maintenance script runs before
  it opens the database. It judges the schema (``core/schema_guard.py``); when migrations are pending it checks
  the upgrade path, takes a BACKUP of the database (``db/migration_backup.py``; a failed backup stops the
  migration), applies them and records the result in ``schema_info``. A database that is at the head changes
  nothing and takes no backup; one that is ahead by expand-only migrations (the rollback of one minor) is left
  alone; one that holds a contract migration this code does not have is refused.
- :func:`require_current_schema` is the start check of the API and the workers: they never migrate, they
  refuse to start on a schema that is behind (``init`` has not run) or that they cannot use.

``MIGRATION_BACKUP=off`` turns the backup off for whoever backs the database up otherwise;
``MIGRATION_BACKUPS_KEEP`` (default 5) is how many are kept; they live in ``DATA_DIR/backups``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.engine import Engine

from core import schema_guard
from core.schema_guard import PHASE_CONTRACT, PHASE_EXPAND, SchemaRecord, Verdict
from db import migration_backup, schema_state
from db.engine import DEFAULT_DATA_DIR, SQLITE, engine, upgrade_to_head
from services import app_version

logger = logging.getLogger(__name__)

DEFAULT_BACKUPS_KEEP = 5
_OFF = {"off", "0", "false", "no"}


class SchemaError(RuntimeError):
    """The schema cannot be used by this code, or cannot be migrated safely. The message says what to do; the
    process that raises it does not start."""


def backups_enabled() -> bool:
    return os.environ.get("MIGRATION_BACKUP", "").strip().lower() not in _OFF


def backups_keep() -> int:
    raw = os.environ.get("MIGRATION_BACKUPS_KEEP", "").strip()
    if not raw:
        return DEFAULT_BACKUPS_KEEP
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("MIGRATION_BACKUPS_KEEP=%r is not a number: keeping %d backups.", raw, DEFAULT_BACKUPS_KEEP)
        return DEFAULT_BACKUPS_KEEP


def backups_directory() -> Path:
    return Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)) / "backups"


def _verdict(db_engine: Engine, version: str, directory: Path) -> tuple[Verdict, SchemaRecord | None, frozenset[str]]:
    applied = schema_state.applied_revisions(db_engine)
    record = schema_state.read_record(db_engine)
    catalog = schema_state.migrations(db_engine)
    verdict = schema_guard.judge(
        applied, catalog.keys(), schema_state.code_heads(db_engine), record, version,
        backups=migration_backup.list_backups(directory),
    )
    return verdict, record, applied


def require_current_schema(db_engine: Engine = engine, *, version: str | None = None,
                           directory: Path | None = None) -> Verdict:
    """The start check of the API and the workers. Returns the verdict when this code can run on the database
    (it is up to date, or ahead by expand-only migrations); raises :class:`SchemaError` otherwise."""
    verdict, _record, _applied = _verdict(
        db_engine, version or app_version.VERSION, directory or backups_directory(),
    )
    if verdict.status == schema_guard.ROLLBACK:
        logger.warning(verdict.message)
    elif verdict.status in (schema_guard.PENDING, schema_guard.TOO_NEW):
        raise SchemaError(verdict.message)
    else:
        logger.info(verdict.message)
    return verdict


def init_db(
    db_engine: Engine = engine,
    *,
    version: str | None = None,
    directory: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Verdict:
    """Bring the database to the head of the migrations, backing it up first when something is pending.
    Returns the verdict it acted on; raises :class:`SchemaError` (the backup failed, the upgrade path does not
    allow it, the schema is one this code cannot use) or the migration's own error."""
    version = version or app_version.VERSION
    directory = directory or backups_directory()
    url = db_engine.url
    if url.get_backend_name() == SQLITE and migration_backup.sqlite_file(url) is not None:
        migration_backup.sqlite_file(url).parent.mkdir(parents=True, exist_ok=True)

    verdict, record, applied = _verdict(db_engine, version, directory)
    if verdict.status == schema_guard.TOO_NEW:
        raise SchemaError(verdict.message)
    if verdict.status == schema_guard.ROLLBACK:
        logger.warning(verdict.message)
        return verdict
    if verdict.status == schema_guard.CURRENT:
        logger.info(verdict.message)
        if record is None:  # at the head with no row: migrated by hand, or restored from before the guard
            _record(db_engine, version, record, ran_contract=False)
        return verdict

    problem = schema_guard.upgrade_path_problem(record, version) if schema_state.has_tables(db_engine) else None
    if problem is not None:  # (an empty database has no past to go through)
        raise SchemaError(problem)

    catalog = schema_state.migrations(db_engine)
    todo = schema_state.pending(applied, catalog)
    contract = [m.revision for m in todo if m.phase == PHASE_CONTRACT]
    logger.info(
        "%d migration(s) pending (%s -> %s)%s.", len(todo), schema_guard.revision_label(applied),
        schema_guard.revision_label(schema_state.code_heads(db_engine)),
        f", {len(contract)} of them contract: {', '.join(contract)}" if contract else "",
    )

    backup = _back_up(db_engine, applied, directory, now())
    try:
        upgrade_to_head(db_engine)
    except Exception:
        logger.error(
            "The migration failed.%s", f" The database as it was before it is in {backup}: see docs/migrations.md "
            "to restore it." if backup else " No backup was taken (MIGRATION_BACKUP=off, or an empty database).",
        )
        raise
    _record(db_engine, version, record, ran_contract=bool(contract))
    return verdict


def _back_up(db_engine: Engine, applied: frozenset[str], directory: Path, now: datetime) -> Optional[Path]:
    """The backup before the migration; None when none is due (switched off, or nothing in the database).
    A backup that fails stops the migration."""
    if not backups_enabled():
        logger.warning("MIGRATION_BACKUP=off: the database is migrated without a backup.")
        return None
    if not schema_state.has_tables(db_engine):
        logger.info("The database is empty: nothing to back up.")
        return None
    source = schema_guard.revision_label(applied) if applied else schema_guard.UNVERSIONED
    target = schema_guard.revision_label(schema_state.code_heads(db_engine))
    try:
        path = migration_backup.backup_database(db_engine.url, directory, source=source, target=target, now=now)
    except migration_backup.BackupError as exc:
        raise SchemaError(
            f"The database was NOT migrated because its backup failed: {str(exc).rstrip('.')}. Fix that and run "
            "init again, or, if you back the database up yourself, set MIGRATION_BACKUP=off."
        ) from exc
    if path is None:
        logger.info("The database is in memory: nothing to back up.")
        return None
    removed = migration_backup.prune_backups(directory, backups_keep())
    logger.info("Backup taken before migrating: %s%s.", path, f" (pruned {len(removed)} older)" if removed else "")
    return path


def _record(db_engine: Engine, version: str, previous: SchemaRecord | None, *, ran_contract: bool) -> None:
    """Write ``schema_info`` after a run: where the database is, who migrated it and what can run on it."""
    heads = schema_state.applied_revisions(db_engine)
    catalog = schema_state.migrations(db_engine)
    schema_state.write_record(db_engine, SchemaRecord(
        revision="+".join(sorted(heads)),
        app_version=version,
        min_app_version=schema_guard.min_app_version_after(
            previous.min_app_version if previous else None, ran_contract, version,
        ),
        contract_revision=schema_state.newest_contract(heads, catalog),
        phase=PHASE_CONTRACT if ran_contract else PHASE_EXPAND,
        migrated_at=datetime.now(timezone.utc),
    ))
