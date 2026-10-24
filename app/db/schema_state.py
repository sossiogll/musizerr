"""
The facts about a database's schema (ADR-0007 §4): the revisions it is at, the ones the code has with the
``phase`` each migration declares, and the ``schema_info`` row. Facts only; ``core/schema_guard.py`` judges them
and ``services/database_init.py`` acts.

``Engine``s come in as arguments: this module never touches the application's own, so a test (or a script)
asks about any database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from alembic.script import ScriptDirectory
from alembic.script.revision import Revision
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session

from core.schema_guard import PHASE_CONTRACT, PHASES, SchemaRecord
from db.engine import alembic_config
from db.models import SCHEMA_INFO_KEY, SchemaInfo
from db.upsert import upsert

SCHEMA_INFO_TABLE = SchemaInfo.__tablename__
VERSION_TABLE = "alembic_version"


@dataclass(frozen=True)
class Migration:
    """One migration of the code, with what its module declares."""

    revision: str
    phase: Optional[str]          # "expand" / "contract"; None: the module declares none (a test fails on it)
    irreversible: bool            # its downgrade cannot be relied on to restore the previous state
    down: tuple[str, ...]


def _downs(revision: Revision) -> tuple[str, ...]:
    down = revision.down_revision
    if down is None:
        return ()
    return (down,) if isinstance(down, str) else tuple(down)


def script_directory(db_engine: Engine) -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config(db_engine.url))


def migrations(db_engine: Engine) -> dict[str, Migration]:
    """Every migration of the code by revision, newest first (the order Alembic walks them)."""
    found: dict[str, Migration] = {}
    for revision in script_directory(db_engine).walk_revisions():
        module = revision.module
        phase = getattr(module, "phase", None)
        found[revision.revision] = Migration(
            revision.revision,
            phase if isinstance(phase, str) else None,
            getattr(module, "irreversible", False) is True,
            _downs(revision),
        )
    return found


def code_heads(db_engine: Engine) -> frozenset[str]:
    return frozenset(script_directory(db_engine).get_heads())


def has_tables(db_engine: Engine) -> bool:
    """The database holds something: a first start on an empty one has nothing to back up."""
    return bool(inspect(db_engine).get_table_names())


def applied_revisions(db_engine: Engine) -> frozenset[str]:
    """The revisions the database is at (the rows of ``alembic_version``); none for a database that was never
    migrated."""
    if VERSION_TABLE not in inspect(db_engine).get_table_names():
        return frozenset()
    with db_engine.connect() as connection:
        return frozenset(row[0] for row in connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")))


def ancestors(revisions: frozenset[str], catalog: dict[str, Migration]) -> set[str]:
    """``revisions`` and every migration below them (the ones already applied when the database is at them)."""
    seen: set[str] = set()
    stack = [revision for revision in revisions if revision in catalog]
    while stack:
        revision = stack.pop()
        if revision in seen:
            continue
        seen.add(revision)
        stack.extend(catalog[revision].down)
    return seen


def pending(applied: frozenset[str], catalog: dict[str, Migration]) -> list[Migration]:
    """The migrations a database at ``applied`` has still to run, oldest first."""
    done = ancestors(applied, catalog)
    return [migration for migration in reversed(list(catalog.values())) if migration.revision not in done]


def newest_contract(revisions: frozenset[str], catalog: dict[str, Migration]) -> Optional[str]:
    """The newest contract migration at or below ``revisions``."""
    done = ancestors(revisions, catalog)
    for migration in catalog.values():  # newest first
        if migration.revision in done and migration.phase == PHASE_CONTRACT:
            return migration.revision
    return None


def undeclared(catalog: dict[str, Migration]) -> list[str]:
    """The migrations that declare no valid phase."""
    return sorted(m.revision for m in catalog.values() if m.phase not in PHASES)


def read_record(db_engine: Engine) -> Optional[SchemaRecord]:
    """The ``schema_info`` row, or None: no table yet (a database from before the guard) or no row."""
    if SCHEMA_INFO_TABLE not in inspect(db_engine).get_table_names():
        return None
    with Session(db_engine) as session:
        row = session.get(SchemaInfo, SCHEMA_INFO_KEY)
        if row is None:
            return None
        return SchemaRecord(
            revision=row.revision,
            app_version=row.app_version,
            min_app_version=row.min_app_version,
            contract_revision=row.contract_revision,
            phase=row.phase,
            migrated_at=row.migrated_at,
        )


def write_record(db_engine: Engine, record: SchemaRecord) -> None:
    """Writes the row (it replaces the one there; the database arbitrates two writers, ``db/upsert.py``)."""
    row = SchemaInfo(
        revision=record.revision,
        app_version=record.app_version,
        min_app_version=record.min_app_version,
        contract_revision=record.contract_revision,
        phase=record.phase,
    )
    if record.migrated_at is not None:
        row.migrated_at = record.migrated_at
    with Session(db_engine) as session:
        upsert(
            session, row, conflict=["id"],
            update=["revision", "app_version", "min_app_version", "contract_revision", "phase", "migrated_at"],
        )
        session.commit()
