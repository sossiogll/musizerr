"""
Migration 0044 (ADR-0010 row 21): a duplicate member the application put in the library carried the
origin of the old product name; it now carries the new one, and every other origin is left alone.
Run on SQLite and on PostgreSQL alike (the suite's dialect).
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.credentials_support import raw, raw_scalar
from tests.database import migration_database

BEFORE, HEAD = "0043_schema_info", "0044_rename_duplicate_origin"
NOW = datetime(2026, 10, 25, 12, 0)
OLD, NEW = "musizerr", "musizeer"  # the old and the new origin value


@pytest.fixture
def db():
    return migration_database(BEFORE)


def _seed(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO duplicate_groups (id, recording_id, status, created_at, updated_at) "
            "VALUES ('g1', 'rec-1', 'proposed', :n, :n)"), {"n": NOW})
        for member_id, origin in (("m1", OLD), ("m2", "unknown"), ("m3", OLD), ("m4", NEW)):
            conn.execute(sa.text(
                "INSERT INTO duplicate_members (id, group_id, sync_id, path, title, artist, origin) "
                "VALUES (:id, 'g1', :sync, :path, 't', 'a', :origin)"),
                {"id": member_id, "sync": f"s-{member_id}", "path": f"a/{member_id}.flac", "origin": origin})


def _origins(engine) -> dict[str, str]:
    return dict(raw(engine, "SELECT id, origin FROM duplicate_members"))


def test_the_upgrade_rewrites_only_the_old_origin(db):
    engine, cfg = db
    _seed(engine)

    command.upgrade(cfg, HEAD)

    assert _origins(engine) == {"m1": NEW, "m2": "unknown", "m3": NEW, "m4": NEW}
    assert raw_scalar(engine, "SELECT version_num FROM alembic_version") == HEAD


def test_running_it_twice_changes_nothing(db):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)
    first = _origins(engine)

    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, HEAD)

    assert _origins(engine) == first


def test_the_downgrade_writes_the_old_value_back(db):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)

    command.downgrade(cfg, BEFORE)

    assert _origins(engine) == {"m1": OLD, "m2": "unknown", "m3": OLD, "m4": OLD}


def test_a_database_without_duplicates_migrates(db):
    engine, cfg = db

    command.upgrade(cfg, HEAD)

    assert _origins(engine) == {}
