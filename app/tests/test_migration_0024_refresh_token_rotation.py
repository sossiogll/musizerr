"""
Migration 0024: ``refresh_tokens.family_id`` and ``replaced_by_id``. The
tokens issued before stay valid (NULL family: each is its own) and the
downgrade removes both columns.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 9, 29, 12, 0)



@pytest.fixture
def migrated():
    engine, cfg = migration_database("0023_download_retriable")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
            "VALUES ('u', 'alice', 'x', 'user', true, :now)"), {"now": NOW})
        conn.execute(sa.text(
            "INSERT INTO refresh_tokens (id, user_id, token_hash, issued_at, expires_at) "
            "VALUES ('rt', 'u', 'h', :now, :exp)"), {"now": NOW, "exp": NOW + timedelta(days=7)})
    command.upgrade(cfg, "0024_refresh_token_rotation")
    return engine, cfg


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("refresh_tokens")}


def test_existing_tokens_keep_working_with_no_family(migrated):
    engine, _ = migrated
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT family_id, replaced_by_id, revoked_at FROM refresh_tokens WHERE id = 'rt'")).one()
    assert tuple(row) == (None, None, None)


def test_downgrade_drops_the_columns(migrated):
    engine, cfg = migrated
    assert {"family_id", "replaced_by_id"} <= _columns(engine)
    command.downgrade(cfg, "0023_download_retriable")
    engine.dispose()
    assert not {"family_id", "replaced_by_id"} & _columns(engine)
