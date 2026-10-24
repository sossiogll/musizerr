"""
Moves an existing SQLite database to PostgreSQL: every row of every table,
primary keys kept, so nothing has to be configured again afterwards.

Uso, dalla cartella ``app/``::

    python -m scripts.migrate_sqlite_to_postgres --source /data/musizeer.db \\
        --target postgresql+psycopg://user:password@postgres:5432/musizeer          # dry run
    python -m scripts.migrate_sqlite_to_postgres --source ... --target ... --apply  # copies
    ... --json                                                                      # machine report

``--target`` defaults to ``DATABASE_URL``. The SQLite file is only read (it is
opened read-only) and is never changed: going back is starting the stack
without ``DATABASE_URL``. Stop the stack first, so nothing writes while it is
copied.

Default is a dry run: it reads the whole source, converts every value as the
copy would, and reports what would be copied and what stops it, without
touching the target. ``--apply``:

1. the target is brought to the Alembic head (the source must already be at
   it: start the stack once on SQLite, or run the migrations on it, first);
2. a target that held data BEFORE the run is refused, unless ``--replace`` (it
   empties the tables first). A target that was empty is the normal case: the
   rows the migrations themselves seeded (migration 0010 imports
   ``MUSIZEER_DATA_DIR/config.json`` into ``settings``) are not data the operator
   holds, the source is authoritative, so they are emptied in the copy's
   transaction and reported;
3. every table is copied, in foreign key order, in ONE transaction;
4. in the same transaction every table is read back and compared with what
   was sent (row count and a checksum over every column of every row); the
   stored credentials are checked to be openable, with the configured
   MUSIZEER_CREDENTIALS_KEY, into JSON. Any difference rolls
   the whole copy back (non-zero exit, the target stays empty);
5. only then the transaction commits.

Types: SQLite 0/1 become booleans, the text of a datetime a ``timestamp``
(UTC, as the application stores them), everything through the types of the
models. The credentials are copied as they are, sealed (docs/credentials.md):
the target needs the same MUSIZEER_CREDENTIALS_KEY the source was sealed with, which
this command requires to be set.

SQLite never checked foreign keys, PostgreSQL does. A row that points at
nothing is reported: a reference that only remembers something (the requester
of a download) is set to NULL, the user of an instance with authentication off
(``SYSTEM_USER_ID``) is created, a refresh token of a user that no longer
exists (a dead session) is dropped; any other orphan stops the copy unless
``--discard-orphans`` drops those rows (and the rows that pointed at them).

Exit code: 0 done (or a clean dry run), 1 the copy failed or did not verify
(rolled back), 2 refused: a precondition does not hold, nothing was touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import URL, Connection, Engine  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from core import environment  # noqa: E402
from core.secret_box import Unreadable  # noqa: E402
from core.utc import as_utc  # noqa: E402
from db import credential_cipher, models, user_service  # noqa: E402
from db.engine import POSTGRESQL, head_revisions, make_engine, parse_url, upgrade_to_head  # noqa: E402

BATCH = 1000
_MODULUS = 1 << 256

# The foreign keys whose NULL means "no reference" and whose row is worth
# keeping when the referenced row is gone: who asked for a download, the
# library track of a like or a file, the download of a duplicate. (A NULL
# user_id of a setting or a credential means "global": those are never
# nulled.)
NULL_ON_ORPHAN = frozenset({
    "downloads.requested_by",
    "user_saved_tracks.library_track_id",
    "library_entries.track_id",
    "duplicate_members.download_id",
})

# A session of a user that no longer exists is a dead session: its rows are
# dropped, reported, without asking (``--discard-orphans`` is for user data).
DROP_ON_ORPHAN = frozenset({"refresh_tokens.user_id"})

# Every table of the schema, parents before the tables that point at them.
APP_TABLES: list[sa.Table] = list(SQLModel.metadata.sorted_tables)
USERS = models.User.__tablename__


class Refusal(Exception):
    """A precondition does not hold: nothing was touched."""


class CopyFailed(Exception):
    """A table could not be read or written."""


@dataclass
class TableReport:
    table: str
    source_rows: int = 0
    copied: int = 0
    discarded: int = 0
    created: int = 0  # rows the source did not have (the system user)
    nulled_references: int = 0
    target_rows: Optional[int] = None
    checksum_match: Optional[bool] = None


@dataclass
class OrphanReport:
    table: str
    column: str
    action: str  # "nulled" | "discarded" | "blocking"
    rows: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass
class Report:
    mode: str
    source: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    tables: list[TableReport] = field(default_factory=list)
    orphans: list[OrphanReport] = field(default_factory=list)
    sequences_reset: list[str] = field(default_factory=list)
    # Rows (per table) the migrations seeded in a target that was empty, which the source replaced.
    seeded_by_migrations: dict[str, int] = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    committed: bool = False
    ok: bool = False
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def open_source(path: Path) -> Engine:
    """The SQLite file, read-only: it is never written."""
    if not path.is_file():
        raise Refusal(f"the source {path} is not a file")
    return sa.create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")


def _revision(connection: Connection) -> Optional[str]:
    if not sa.inspect(connection).has_table("alembic_version"):
        return None
    return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


def check_source(source: Connection, report: Report) -> None:
    heads = head_revisions()
    revision = _revision(source)
    report.source.update(revision=revision, head=sorted(heads))
    if revision not in heads:
        report.problems.append(
            f"the source is at revision {revision!r}, the application is at {sorted(heads)}: upgrade the "
            "source first (start the stack on SQLite once, or run the migrations on it), then copy it"
        )
    tables = set(sa.inspect(source).get_table_names())
    missing = [table.name for table in APP_TABLES if table.name not in tables]
    if missing:
        report.problems.append(f"the source lacks the tables {missing}")
    for name in sorted(tables - {table.name for table in APP_TABLES} - {"alembic_version"}):
        rows = source.execute(sa.text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
        if rows:
            report.problems.append(f"the source table {name!r} ({rows} rows) is not part of the schema: it would be lost")


# ---------------------------------------------------------------------------
# Rows: what the target takes, and the checksum of it
# ---------------------------------------------------------------------------


def _canonical(value):
    if isinstance(value, datetime):
        return as_utc(value).replace(tzinfo=None).isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def _row_digest(values: dict) -> int:
    payload = json.dumps(sorted((key, _canonical(value)) for key, value in values.items()), default=str)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest(), "big")


def table_checksum(connection: Connection, table: sa.Table) -> tuple[int, int]:
    """(rows, checksum) of a table: the checksum adds the digest of every row
    (every column, normalized), so it does not depend on the order the two
    databases sort in."""
    rows = checksum = 0
    for row in connection.execute(sa.select(table).execution_options(yield_per=BATCH)):
        rows += 1
        checksum = (checksum + _row_digest(dict(row._mapping))) % _MODULUS
    return rows, checksum


class _Rows:
    """The rows of the source in the form the target takes, table by table:
    which of them stay, and which references they keep."""

    def __init__(self, discard_orphans: bool) -> None:
        self.discard_orphans = discard_orphans
        self.orphans: dict[tuple[str, str], OrphanReport] = {}
        # The values the tables that are pointed at have kept, as they go.
        referred = {(fk.column.table.name, fk.column.name) for table in APP_TABLES for fk in table.foreign_keys}
        self.known: dict[tuple[str, str], set] = {key: set() for key in referred}
        self.known[(USERS, "id")].add(models.SYSTEM_USER_ID)
        self.checksums: dict[str, int] = {}

    def _orphan(self, table: sa.Table, child: sa.Column, values: dict) -> str:
        """What to do with a row whose ``child`` points at nothing: "null",
        "discard" or "block"."""
        key = f"{table.name}.{child.name}"
        if key in NULL_ON_ORPHAN:
            action, label = "null", "nulled"
        elif key in DROP_ON_ORPHAN or self.discard_orphans:
            action, label = "discard", "discarded"
        else:
            action, label = "block", "blocking"
        report = self.orphans.setdefault((table.name, child.name), OrphanReport(table.name, child.name, label))
        report.rows += 1
        if len(report.examples) < 3:
            report.examples.append(f"{[values[c.name] for c in table.primary_key.columns]} -> {values[child.name]!r}")
        return action

    def _rows_of(self, source: Connection, table: sa.Table, report: TableReport) -> Iterator[dict]:
        foreign = [(fk.parent, (fk.column.table.name, fk.column.name)) for fk in table.foreign_keys]
        has_system_user = False
        for row in source.execute(sa.select(table).execution_options(yield_per=BATCH)):
            values = dict(row._mapping)
            report.source_rows += 1
            keep = True
            for child, parent_key in foreign:
                value = values[child.name]
                if value is None or value in self.known[parent_key]:
                    continue
                action = self._orphan(table, child, values)
                if action == "null":
                    values[child.name] = None
                    report.nulled_references += 1
                elif action == "discard":
                    keep = False
            if not keep:
                report.discarded += 1
                continue
            has_system_user = has_system_user or (table.name == USERS and values["id"] == models.SYSTEM_USER_ID)
            yield values
        if table.name == USERS and not has_system_user:
            # The user of an instance with authentication off is not an account: a source that
            # never needed its row gets it here, whoever points at it.
            report.created += 1
            yield {column.key: getattr(user_service.system_user(), column.key) for column in table.columns}

    def iterate(self, source: Connection, table: sa.Table, report: TableReport) -> Iterator[dict]:
        """The rows of ``table`` that go to the target, counted in ``report``."""
        referred = [column for (name, column) in self.known if name == table.name]
        checksum = 0
        for values in self._rows_of(source, table, report):
            for column in referred:
                self.known[(table.name, column)].add(values[column])
            report.copied += 1
            checksum = (checksum + _row_digest(values)) % _MODULUS
            yield values
        self.checksums[table.name] = checksum


def _batches(rows: Iterable[dict]) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def read_source(
    source: Connection, discard_orphans: bool, write: Optional[Callable[[sa.Table, list[dict]], None]] = None,
) -> tuple[list[TableReport], _Rows]:
    """Reads every table of the source in foreign key order, handing the
    batches to ``write`` (nothing: a dry run)."""
    rows = _Rows(discard_orphans)
    tables: list[TableReport] = []
    for table in APP_TABLES:
        report = TableReport(table.name)
        tables.append(report)
        try:
            for batch in _batches(rows.iterate(source, table, report)):
                if write is not None:
                    write(table, batch)
        except Exception as error:  # noqa: BLE001 — name the table, whatever the driver says
            raise CopyFailed(f"{table.name}: {type(error).__name__}: {str(error).splitlines()[0]}") from error
    return tables, rows


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def open_target(url: URL) -> Engine:
    if url.get_backend_name() != POSTGRESQL:
        raise Refusal("the target must be a PostgreSQL URL (postgresql+psycopg://user:password@host:5432/database)")
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # noqa: BLE001
        engine.dispose()
        raise Refusal(f"cannot connect to the target: {type(error).__name__}: {str(error).splitlines()[0]}") from error
    return engine


def target_state(connection: Connection) -> dict:
    """What the target holds now: its revision and the tables with rows."""
    inspector = sa.inspect(connection)
    rows = {
        table.name: connection.execute(sa.select(sa.func.count()).select_from(table)).scalar()
        for table in APP_TABLES if inspector.has_table(table.name)
    }
    return {"revision": _revision(connection), "rows": {name: count for name, count in rows.items() if count}}


def reset_sequences(connection: Connection, tables: Iterable[sa.Table] = APP_TABLES) -> list[str]:
    """Sequences and identity columns continue after the highest id copied.
    Every table of the schema has a text primary key today, so this finds
    none; a table that gets an integer one is covered."""
    preparer = connection.dialect.identifier_preparer
    reset = []
    for table in tables:
        keys = list(table.primary_key.columns)
        if len(keys) != 1 or not isinstance(keys[0].type, sa.Integer):
            continue
        sequence = connection.execute(
            sa.text("SELECT pg_get_serial_sequence(:table, :column)"), {"table": table.name, "column": keys[0].name},
        ).scalar()
        if sequence is None:
            continue
        connection.execute(
            sa.text(f"SELECT setval(:sequence, COALESCE((SELECT MAX({preparer.quote(keys[0].name)}) "
                    f"FROM {preparer.format_table(table)}), 0) + 1, false)"),
            {"sequence": sequence},
        )
        reset.append(f"{table.name}.{keys[0].name}")
    return reset


def check_credentials(connection: Connection) -> dict:
    """The stored credentials are sealed and the configured keys open each one into a JSON object.
    (They are copied as they are, sealed: a target that cannot open them would hold accounts
    nobody can use.)"""
    box = credential_cipher.box()
    checked = unreadable = 0
    for (data,) in connection.execute(sa.select(models.ProviderCredential.__table__.c.data)):
        checked += 1
        try:
            unreadable += not isinstance(json.loads(box.open(data)), dict)
        except (Unreadable, ValueError):
            unreadable += 1
    return {"checked": checked, "unreadable": unreadable}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def migrate(
    source_path: Path, target_url: URL, *, apply: bool, replace: bool = False, discard_orphans: bool = False,
) -> Report:
    report = Report(mode="apply" if apply else "dry-run")
    try:
        _migrate(source_path, target_url, report, apply=apply, replace=replace, discard_orphans=discard_orphans)
    except Refusal as refusal:
        report.problems.append(str(refusal))
        report.exit_code = 2
    except CopyFailed as failure:
        report.problems.append(f"the copy failed and was rolled back: {failure}")
        report.exit_code = 1
    report.ok = not report.problems
    if report.problems and not report.exit_code:
        report.exit_code = 2
    return report


def _migrate(
    source_path: Path, target_url: URL, report: Report, *, apply: bool, replace: bool, discard_orphans: bool,
) -> None:
    try:
        credential_cipher.require_credentials_key()
    except credential_cipher.CredentialsKeyError as refused:
        raise Refusal(str(refused)) from None
    source_engine = open_source(source_path)
    target_engine = open_target(target_url)
    report.source["path"] = str(source_path)
    report.target["url"] = target_url.render_as_string(hide_password=True)
    try:
        with source_engine.connect() as source:
            check_source(source, report)
            with target_engine.connect() as target:
                state = target_state(target)
            report.target.update(state, replace=replace)
            if state["rows"] and not replace:
                report.problems.append(
                    f"the target already holds data ({sum(state['rows'].values())} rows in {sorted(state['rows'])}): "
                    "use an empty database, or --replace to empty it first"
                )
            if report.problems:
                return

            # What the copy would do, without a write to the target.
            try:
                report.tables, rows = read_source(source, discard_orphans)
            except CopyFailed as failure:
                raise Refusal(f"cannot read the source: {failure}") from failure
            report.orphans = list(rows.orphans.values())
            report.problems += [
                f"{o.rows} rows of {o.table}.{o.column} point at nothing (e.g. {o.examples[0]}): "
                "--discard-orphans drops them"
                for o in report.orphans if o.action == "blocking"
            ]
            if report.problems or not apply:
                return

            try:
                upgrade_to_head(target_engine)
            except Exception as error:  # noqa: BLE001
                raise Refusal(
                    f"cannot bring the target to the head: {type(error).__name__}: {str(error).splitlines()[0]}"
                ) from error
            _apply(source, target_engine, report, discard_orphans=discard_orphans)
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _apply(source: Connection, target_engine: Engine, report: Report, *, discard_orphans: bool) -> None:
    with target_engine.begin() as target:
        # Whatever the tables hold now is replaced by the source: the data of a target that
        # held some before the run (``--replace``; without it ``_migrate`` refused), or the rows
        # the migrations seeded in one that was empty.
        present = target_state(target)["rows"]
        if present:
            names = ", ".join(target.dialect.identifier_preparer.format_table(table) for table in APP_TABLES)
            target.execute(sa.text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
            if not report.target["rows"]:
                report.seeded_by_migrations = present

        report.tables, rows = read_source(source, discard_orphans, lambda table, batch: target.execute(table.insert(), batch))
        report.sequences_reset = reset_sequences(target)
        for table_report, table in zip(report.tables, APP_TABLES):
            table_report.target_rows, checksum = table_checksum(target, table)
            table_report.checksum_match = checksum == rows.checksums[table.name]
        report.credentials = check_credentials(target)

        different = [t.table for t in report.tables if not t.checksum_match or t.target_rows != t.copied]
        if different:
            raise CopyFailed(f"what the target holds differs from what was sent: {different}")
        if report.credentials["unreadable"]:
            raise CopyFailed(
                f"{report.credentials['unreadable']} stored credentials cannot be opened with the configured "
                f"{credential_cipher.ENV_KEY} / {credential_cipher.ENV_PREVIOUS}"
            )
    report.committed = True
    with target_engine.connect() as target:
        # Read again after the commit, from a connection of its own.
        held = target_state(target)["rows"]
    report.problems += [
        f"{t.table}: the committed rows differ from the verified ones" for t in report.tables
        if held.get(t.table, 0) != t.target_rows
    ]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def format_report(report: Report) -> str:
    lines = [f"Mode: {report.mode}"]
    lines.append(f"Source: {report.source.get('path')} (revision {report.source.get('revision')}, head {report.source.get('head')})")
    held = sum(report.target.get("rows", {}).values())
    lines.append(f"Target: {report.target.get('url')} (revision {report.target.get('revision')}, {held} rows)")
    if report.tables:
        width = max(len(t.table) for t in report.tables)
        lines.append(f"{'table'.ljust(width)}  {'source':>8} {'copied':>8} {'dropped':>8} {'nulled':>7} {'created':>8} {'target':>8}  checksum")
        for t in report.tables:
            match = "" if t.checksum_match is None else ("ok" if t.checksum_match else "DIFFERENT")
            target = "" if t.target_rows is None else t.target_rows
            lines.append(
                f"{t.table.ljust(width)}  {t.source_rows:>8} {t.copied:>8} {t.discarded:>8} {t.nulled_references:>7} "
                f"{t.created:>8} {target!s:>8}  {match}"
            )
    for orphan in report.orphans:
        lines.append(f"orphans: {orphan.rows} rows of {orphan.table}.{orphan.column} {orphan.action}, e.g. {orphan.examples}")
    if report.seeded_by_migrations:
        seeded = ", ".join(f"{table}: {rows}" for table, rows in report.seeded_by_migrations.items())
        lines.append(
            f"{sum(report.seeded_by_migrations.values())} rows seeded by the migrations ({seeded}) replaced by the source"
        )
    if report.sequences_reset:
        lines.append(f"sequences reset: {report.sequences_reset}")
    if report.credentials:
        lines.append(f"credentials: {report.credentials['checked']} checked, {report.credentials['unreadable']} unreadable")
    for problem in report.problems:
        lines.append(f"PROBLEM: {problem}")
    if report.ok:
        lines.append("Result: " + ("committed, verified" if report.committed else "dry run: the copy would go ahead"))
    else:
        lines.append(f"Result: FAILED (exit {report.exit_code})")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="the SQLite database file")
    parser.add_argument("--target", help="the PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--apply", action="store_true", help="copy (default: a dry run)")
    parser.add_argument("--replace", action="store_true", help="empty the target tables first")
    parser.add_argument("--discard-orphans", action="store_true", help="drop the rows that point at nothing")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    target = args.target or environment.get("DATABASE_URL")
    if not target:
        parser.error("--target (or DATABASE_URL) is required")
    report = migrate(
        args.source.resolve(), parse_url(target),
        apply=args.apply, replace=args.replace, discard_orphans=args.discard_orphans,
    )
    print(json.dumps(asdict(report), indent=2, default=str) if args.json else format_report(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
