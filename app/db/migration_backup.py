"""
The backup taken before a migration (ADR-0007 §4), and the restore that undoes it.

- PostgreSQL: ``pg_dump`` in custom format (``pg_restore`` reads it back). The password goes in the environment
  of the child process, never on its command line. The image carries the client of the server's major
  (postgresql-client-17, the ``Dockerfile``); a client older than the server refuses, and the message says so.
- SQLite: the online backup API of the ``sqlite3`` module: a consistent snapshot of the file, WAL included,
  whoever is writing to it.

The file is written under a temporary name and renamed when complete, so a failed backup leaves no file that looks
like one. It holds the whole database (sealed credentials, password hashes): mode 600, in a folder of mode 700.

Names, order and pruning are ``core/schema_guard.py``'s.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from core import schema_guard
from db.engine import POSTGRESQL, SQLITE, make_engine

logger = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".partial"
EXTENSION = {POSTGRESQL: "dump", SQLITE: "sqlite3"}


class BackupError(RuntimeError):
    """The backup could not be taken (or restored). The message says what to do about it."""


def sqlite_file(url: URL) -> Optional[Path]:
    """The file of a SQLite database; None for one in memory."""
    return None if url.database in (None, "", ":memory:") else Path(url.database)


def _libpq_target(url: URL) -> tuple[str, dict[str, str]]:
    """The connection string of a PostgreSQL URL for ``pg_dump``/``pg_restore`` without its password, and the
    environment that carries it."""
    bare = URL.create(
        "postgresql", username=url.username, host=url.host, port=url.port, database=url.database, query=url.query,
    )
    env = {"PGPASSWORD": url.password} if url.password else {}
    return bare.render_as_string(hide_password=True), env


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise BackupError(
            f"{name} is not installed in this image, so a PostgreSQL database cannot be backed up. The Musizeer "
            "image ships postgresql-client-17; use it, or back the database up yourself and set MIGRATION_BACKUP=off."
        )
    return found


def _run(command: list[str], env: dict[str, str], what: str) -> None:
    try:
        done = subprocess.run(
            command, env={**os.environ, **env}, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
    except OSError as exc:
        raise BackupError(f"{what} could not be started: {exc}") from exc
    if done.returncode != 0:
        detail = " ".join(done.stderr.strip().splitlines()[-6:]) or f"exit status {done.returncode}"
        hint = ""
        if "version mismatch" in done.stderr:
            hint = (" The client in this image is older than the server: use a PostgreSQL server of the image's "
                    "major (17), or update the image.")
        raise BackupError(f"{what} failed: {detail}.{hint}")


def _private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # a volume that does not keep modes
        logger.debug("could not set mode %o on %s", mode, path)


def backup_database(url: URL, directory: Path, *, source: str, target: str, now: datetime) -> Optional[Path]:
    """Dump the database at ``url`` into ``directory`` as the backup of the migration from ``source`` to
    ``target`` (revision labels). Returns the file, or None for a database in memory (nothing to keep).
    Raises :class:`BackupError`."""
    backend = url.get_backend_name()
    if backend not in EXTENSION:
        raise BackupError(f"a {backend} database cannot be backed up")
    if backend == SQLITE and sqlite_file(url) is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"the backup folder {directory} cannot be created: {exc}") from exc
    _private(directory, 0o700)

    final = directory / schema_guard.backup_name(now, source, target, EXTENSION[backend])
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    try:
        if backend == SQLITE:
            _dump_sqlite(sqlite_file(url), partial)
        else:
            _dump_postgresql(url, partial)
        _private(partial, 0o600)
        os.replace(partial, final)
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:  # disk full, permissions, a database that cannot be read
        partial.unlink(missing_ok=True)
        raise BackupError(f"the backup {final.name} could not be written to {directory}: {exc}") from exc
    return final


def _dump_postgresql(url: URL, destination: Path) -> None:
    connection, env = _libpq_target(url)
    _run(
        [_tool("pg_dump"), "--format=custom", "--no-password", "--file", str(destination), "--dbname", connection],
        env, "pg_dump",
    )


def _dump_sqlite(path: Path, destination: Path) -> None:
    source = sqlite3.connect(path)
    try:
        copy = sqlite3.connect(destination)
        try:
            source.backup(copy)
            # The pages carry the source's WAL mode: a copy opened later would grow -wal/-shm files beside it.
            # One self-contained file is what a backup should be.
            copy.execute("PRAGMA journal_mode = DELETE").fetchone()
            result = copy.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise BackupError(f"the copy of {path} did not pass SQLite's integrity check: {result}")
        finally:
            copy.close()
    finally:
        source.close()


def prune_backups(directory: Path, keep: int) -> list[str]:
    """Delete the backups beyond the last ``keep`` (and any half-written file a crash left); the names deleted.
    A file that is not one of ours stays."""
    if not directory.is_dir():
        return []
    for leftover in directory.glob(f"*{PARTIAL_SUFFIX}"):
        leftover.unlink(missing_ok=True)
    removed = schema_guard.backups_to_prune([p.name for p in directory.iterdir()], keep)
    for name in removed:
        (directory / name).unlink(missing_ok=True)
    return removed


def list_backups(directory: Path) -> list[str]:
    """The names of the backups in ``directory`` (ours only), oldest first."""
    if not directory.is_dir():
        return []
    parsed = [b for b in map(schema_guard.parse_backup_name, (p.name for p in directory.iterdir())) if b is not None]
    return [b.name for b in sorted(parsed, key=lambda b: (b.stamp, b.name))]


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def check_backup(path: Path, backend: str) -> None:
    """Does ``path`` read as a backup of ``backend``? Raises :class:`BackupError` when it does not."""
    if not path.is_file():
        raise BackupError(f"{path} is not a file")
    if backend == POSTGRESQL:
        _run([_tool("pg_restore"), "--list", str(path)], {}, "pg_restore --list")
        return
    try:
        copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = copy.execute("PRAGMA integrity_check").fetchone()
            tables = copy.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            copy.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path} is not a SQLite database: {exc}") from exc
    if result is None or result[0] != "ok" or not tables:
        raise BackupError(f"{path} is empty or did not pass SQLite's integrity check")


def _empty_public_schema(url: URL) -> None:
    """Drop everything in the database: a backup of an older schema does not name the tables a later migration
    added, so ``pg_restore --clean`` would leave them behind. Fails (the lock wait runs out) when a service is
    still connected."""
    engine = make_engine(url, poolclass=NullPool).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    except SQLAlchemyError as exc:
        raise BackupError(
            f"the database could not be emptied before the restore (is a service still connected to it? stop the "
            f"application, the workers and beat first): {exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        engine.dispose()


def restore_database(url: URL, path: Path) -> None:
    """Replace the content of the database at ``url`` with the backup at ``path``. The services that use the
    database must be stopped. PostgreSQL: the ``public`` schema is emptied, then the backup is restored in one
    transaction (if that fails the schema stays empty: run it again, the backup is still there). SQLite: the
    backup API copies the pages into the live file (its WAL included)."""
    backend = url.get_backend_name()
    if backend == POSTGRESQL:
        connection, env = _libpq_target(url)
        pg_restore = _tool("pg_restore")  # found before anything is dropped
        _empty_public_schema(url)
        _run(
            [pg_restore, "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction", "--no-password",
             "--dbname", connection, str(path)],
            env, "pg_restore",
        )
        return
    live = sqlite_file(url)
    if live is None:
        raise BackupError("a database in memory cannot be restored")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(live)
        try:
            source.backup(destination)
        finally:
            destination.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"the backup could not be restored into {live}: {exc}") from exc
    finally:
        source.close()
