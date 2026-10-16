"""
Migration 0040: ``storage_provider`` loses its catalog default, so the value
the old default stood for is written where no system row exists yet.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 10, 16, 12, 0)
KEY = "storage_provider"
REVISION = "0040_storage_provider_explicit"


@pytest.fixture
def db():
    return migration_database("0039_provider_sources_v2")


def _setting(engine, row_id, scope, user_id, value, key=KEY):
    with engine.begin() as conn:
        if user_id is not None:
            conn.execute(sa.text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :id, 'x', 'user', true, :now)"), {"id": user_id, "now": NOW})
        conn.execute(sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, :scope, :user_id, :value, :now)"),
            {"id": row_id, "key": key, "scope": scope, "user_id": user_id, "value": json.dumps(value), "now": NOW})


def _rows(engine, key=KEY):
    with engine.connect() as conn:
        return sorted(conn.execute(sa.text(
            "SELECT scope, user_id, value FROM settings WHERE key = :key"), {"key": key}).all(),
            key=lambda row: (row[0], row[1] or ""))


def test_0040_writes_the_old_default_where_nothing_was_stored(db):
    engine, cfg = db

    command.upgrade(cfg, REVISION)

    assert _rows(engine) == [("system", None, '"local"')]
    with engine.connect() as conn:
        updated_at = conn.execute(sa.text("SELECT updated_at FROM settings WHERE key = :key"), {"key": KEY}).scalar()
    assert updated_at is not None


def test_0040_keeps_a_value_somebody_chose(db):
    engine, cfg = db
    _setting(engine, "chosen", "system", None, "shelf")

    command.upgrade(cfg, REVISION)

    assert _rows(engine) == [("system", None, '"shelf"')]


def test_0040_a_personal_row_does_not_count_as_the_system_value(db):
    engine, cfg = db
    _setting(engine, "mine", "user", "u1", "elsewhere")

    command.upgrade(cfg, REVISION)

    assert _rows(engine) == [("system", None, '"local"'), ("user", "u1", '"elsewhere"')]


def test_0040_does_not_touch_other_settings(db):
    engine, cfg = db
    _setting(engine, "other", "system", None, True, key="dedup_enabled")

    command.upgrade(cfg, REVISION)

    assert _rows(engine, "dedup_enabled") == [("system", None, "true")]


def test_0040_runs_again_without_adding_a_second_row(db):
    engine, cfg = db
    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, "0039_provider_sources_v2")

    command.upgrade(cfg, REVISION)

    assert _rows(engine) == [("system", None, '"local"')]
