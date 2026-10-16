"""
A clean database for a test, on the dialect under test.

The whole suite runs on SQLite (an in-memory database per call, the default)
or on PostgreSQL (``TEST_DATABASE_URL``, e.g. ``postgresql+psycopg://user:
password@host:5432/postgres``: the user must be allowed to create databases).
No test builds its own engine: ``new_engine()`` hands out a database of its
own and the autouse fixture of ``tests/conftest.py`` drops it when the test
ends.

On PostgreSQL the schema is built once, in a template database, and every
test database is a copy of it (``CREATE DATABASE ... TEMPLATE``): a clean
database costs a file copy, not a ``create_all``. The engine is the
application's (``db.engine.make_engine``): the same timeouts and pool.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Iterable

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool, StaticPool
from sqlmodel import Session, SQLModel

from db import user_service
from db.models import TrackRecord, User
from db.engine import POSTGRESQL, SQLITE, alembic_config, make_engine, sqlite_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
DIALECT = POSTGRESQL if TEST_DATABASE_URL else SQLITE

# What undoes each database handed out to the running test.
_cleanups: dict[Engine, Callable[[], None]] = {}
_template: str | None = None


def new_engine(*, schema: bool = True, addressable: bool = False) -> Engine:
    """An engine on a database of its own, empty when ``schema`` is False
    (a test that runs the migrations), otherwise with every table of the
    models. ``addressable`` is for a database another engine reaches by its
    URL (Alembic): a file on SQLite (a memory database lives in one
    connection), a database on PostgreSQL either way."""
    if DIALECT == POSTGRESQL:
        return _new_postgresql(schema)
    return _new_sqlite(schema, addressable)


def migration_database(revision: str) -> tuple[Engine, Config]:
    """A database migrated up to ``revision`` (every migration before it has
    run) and the Alembic configuration that continues from there: for a test
    that seeds the data of an older schema and migrates on."""
    engine = new_engine(schema=False, addressable=True)
    config = alembic_config(engine.url)
    command.upgrade(config, revision)
    return engine, config


def seed_parents(session: Session, *, users: Iterable[str] = (), tracks: Iterable[str] = ()) -> None:
    """The users and tracks rows other rows point at, under the ids a test
    uses (PostgreSQL enforces the foreign keys SQLite never checked); the
    ones already there are left as they are."""
    for user_id in users:
        session.merge(User(id=user_id, username=user_id, password_hash="x"))
    for track_id in tracks:
        session.merge(TrackRecord(internal_id=track_id, title=track_id, artist="Artist", album="Album"))
    session.commit()


def seed_system_user(engine: Engine) -> None:
    """The row of the user an instance with authentication off writes its
    favorites and likes as (what the application creates at start)."""
    with Session(engine) as session:
        user_service.ensure_system_user(session)


def release_engine(engine: Engine) -> None:
    """Closes the connections of ``engine`` and drops its database. A test
    that makes engines in a loop (a Hypothesis example) releases each one
    when it is done with it: every engine holds a pool of open connections,
    and the server only has so many."""
    _cleanups.pop(engine)()


def release_engines() -> None:
    """Drop what ``new_engine`` handed out since the last call. A connection
    still checked out (a session nobody closed) is a leak, and fails the test."""
    leaked = sum(engine.pool.checkedout() for engine in _cleanups if hasattr(engine.pool, "checkedout"))
    while _cleanups:
        release_engine(next(iter(_cleanups)))
    assert not leaked, f"{leaked} connections were never given back to their pool: a session was not closed"


def _new_sqlite(schema: bool, addressable: bool) -> Engine:
    if addressable:
        folder = tempfile.mkdtemp(prefix="musizerr-test-")
        engine = make_engine(sqlite_url(Path(folder) / "musizerr.db"))
        _cleanups[engine] = lambda: (engine.dispose(), shutil.rmtree(folder, ignore_errors=True))
    else:
        # One connection, shared by every thread: the memory database.
        engine = make_engine("sqlite://", poolclass=StaticPool)
        _cleanups[engine] = engine.dispose
    if schema:
        SQLModel.metadata.create_all(engine)
    return engine


def _admin() -> Engine:
    return sa.create_engine(make_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _database_url(name: str):
    return make_url(TEST_DATABASE_URL).set(database=name)


def _create_database(name: str, template: str | None = None) -> None:
    admin = _admin()
    try:
        with admin.connect() as connection:
            suffix = f' TEMPLATE "{template}"' if template else ""
            connection.execute(sa.text(f'CREATE DATABASE "{name}"{suffix}'))
    finally:
        admin.dispose()


def _drop_database(name: str) -> None:
    admin = _admin()
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _template_database() -> str:
    global _template
    if _template is None:
        _template = f"mz_template_{uuid.uuid4().hex[:12]}"
        _create_database(_template)
        atexit.register(_drop_database, _template)
        engine = make_engine(_database_url(_template))
        SQLModel.metadata.create_all(engine)
        engine.dispose()
    return _template


def _new_postgresql(schema: bool) -> Engine:
    name = f"mz_test_{uuid.uuid4().hex[:12]}"
    _create_database(name, _template_database() if schema else None)
    engine = make_engine(_database_url(name))

    def drop() -> None:
        engine.dispose()
        _drop_database(name)

    _cleanups[engine] = drop
    return engine
