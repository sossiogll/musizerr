"""
Connections are given back: every engine of the test factory closes its pool
when it is released (a Hypothesis example makes one per example: a pool each
left open until the end of the test used up the server's connections), and the
code of the application returns every connection to its pool and opens no
engine of its own.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlmodel import Session, select

from db import engine as db_engine
from db.engine import atomic_session, read_snapshot
from db.models import TrackRecord
from scripts import migrate_sqlite_to_postgres as tool

from tests.database import _admin, new_engine, release_engine
from tests.test_migrate_sqlite_to_postgres import sqlite_source

pytestmark = pytest.mark.postgresql_only("open connections of a PostgreSQL server")


def _open_connections(database: str | None = None) -> int:
    """Connections open to the test databases (or one of them), from the server."""
    admin = _admin()
    try:
        with admin.connect() as connection:
            return connection.execute(
                sa.text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname LIKE :name"),
                {"name": database or "mz_test_%"},
            ).scalar()
    finally:
        admin.dispose()


def _use(engine) -> None:
    with Session(engine) as session:
        session.exec(select(TrackRecord)).all()


def test_the_counter_sees_a_connection_that_is_left_open():
    before = _open_connections()
    engine = new_engine()
    _use(engine)

    assert _open_connections() == before + 1

    release_engine(engine)
    assert _open_connections() == before


def test_engines_made_in_a_loop_leave_no_connection_behind():
    before = _open_connections()

    for _ in range(40):
        engine = new_engine()
        _use(engine)
        release_engine(engine)

    assert _open_connections() == before


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(0, 3))
def test_hypothesis_examples_that_release_their_engine_leak_nothing(_example):
    engine = new_engine()
    _use(engine)
    release_engine(engine)


def test_the_connections_after_many_hypothesis_examples_are_what_they_were():
    before = _open_connections()

    test_hypothesis_examples_that_release_their_engine_leak_nothing()

    assert _open_connections() == before


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


def test_a_dry_run_session_and_a_snapshot_return_their_connection(monkeypatch):
    engine = new_engine(addressable=True)
    monkeypatch.setattr(db_engine, "engine", engine)

    for _ in range(30):
        with atomic_session(commit=False) as session:
            session.exec(select(TrackRecord)).all()
        with Session(engine) as session:
            read_snapshot(session)
            session.exec(select(TrackRecord)).all()

    assert engine.pool.checkedout() == 0
    assert engine.pool.checkedin() <= 1  # the one connection every call reused


def test_an_error_inside_a_dry_run_session_returns_the_connection_too(monkeypatch):
    engine = new_engine(addressable=True)
    monkeypatch.setattr(db_engine, "engine", engine)

    for _ in range(10):
        with pytest.raises(RuntimeError), atomic_session(commit=True):
            raise RuntimeError("stopped")

    assert engine.pool.checkedout() == 0


def test_the_startup_reapers_of_a_worker_return_their_connection(monkeypatch):
    from workers import celery_app

    engine = new_engine(addressable=True)
    monkeypatch.setattr(db_engine, "engine", engine)

    for _ in range(10):
        celery_app._reap_orphaned_running_downloads()
        celery_app._reap_orphaned_library_jobs()

    assert engine.pool.checkedout() == 0
    assert engine.pool.checkedin() <= 1


def test_the_migration_tool_closes_every_engine_it_opens(tmp_path):
    source = sqlite_source(tmp_path)
    target = new_engine(schema=False)
    database = target.url.database

    for apply in (False, True):
        assert tool.migrate(source, target.url, apply=apply, replace=apply).ok
        assert _open_connections(database) == 0

    refused = tool.migrate(tmp_path / "missing.db", target.url, apply=True)
    assert not refused.ok
    assert _open_connections(database) == 0
