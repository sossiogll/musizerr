"""
Migration 0030: ``unlike_on_origin_providers`` becomes a personal setting.

The global value never becomes a default for users who did not choose: when
it was on, each admin keeps it on as their own choice (an admin turned it on,
and it already acted on their accounts) unless they already chose; the global
row goes either way.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 10, 3, 12, 0)
KEY = "unlike_on_origin_providers"



@pytest.fixture
def db():
    engine, cfg = migration_database("0029_library_entry_missing")
    with engine.begin() as conn:
        for user_id, role in (("admin1", "admin"), ("admin2", "admin"), ("user1", "user")):
            conn.execute(sa.text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :id, 'x', :role, true, :now)"), {"id": user_id, "role": role, "now": NOW})
    return engine, cfg


def _setting(conn, row_id, scope, user_id, value, key=KEY):
    conn.execute(sa.text(
        "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
        "VALUES (:id, :key, :scope, :user_id, :value, :now)"),
        {"id": row_id, "key": key, "scope": scope, "user_id": user_id, "value": json.dumps(value), "now": NOW})


def _rows(engine, key=KEY):
    with engine.connect() as conn:
        return sorted(conn.execute(sa.text(
            "SELECT scope, user_id, value FROM settings WHERE key = :key"), {"key": key}).all(),
            key=lambda row: (row[0], row[1] or ""))


def test_0030_an_enabled_global_value_stays_on_only_for_the_admins(db):
    engine, cfg = db
    with engine.begin() as conn:
        _setting(conn, "g", "system", None, True)
        # admin2 already chose for themselves: their choice stands.
        _setting(conn, "a2", "user", "admin2", False)
        _setting(conn, "other", "system", None, True, key="dedup_enabled")

    command.upgrade(cfg, "0030_unlike_on_origin_per_user")

    assert _rows(engine) == [("user", "admin1", "true"), ("user", "admin2", "false")]
    with engine.connect() as conn:
        updated_at = conn.execute(sa.text(
            "SELECT updated_at FROM settings WHERE key = :key AND user_id = 'admin1'"), {"key": KEY}).scalar()
    assert updated_at is not None
    # Other settings are not touched.
    assert _rows(engine, "dedup_enabled") == [("system", None, "true")]


def test_0030_a_disabled_or_missing_global_value_is_dropped_and_nobody_gets_it(db):
    engine, cfg = db
    with engine.begin() as conn:
        _setting(conn, "g", "system", None, False)

    command.upgrade(cfg, "0030_unlike_on_origin_per_user")

    assert _rows(engine) == []


def test_0030_without_any_row_nothing_happens(db):
    engine, cfg = db

    command.upgrade(cfg, "0030_unlike_on_origin_per_user")

    assert _rows(engine) == []


def test_0030_downgrade_turns_the_global_value_back_on_if_anyone_had_it(db):
    engine, cfg = db
    with engine.begin() as conn:
        _setting(conn, "g", "system", None, True)
    command.upgrade(cfg, "0030_unlike_on_origin_per_user")

    command.downgrade(cfg, "0029_library_entry_missing")

    assert _rows(engine) == [("system", None, "true")]
