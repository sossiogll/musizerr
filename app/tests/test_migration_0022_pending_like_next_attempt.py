"""
Migration 0022: ``pending_likes.next_attempt_at`` (nullable, indexed). An
existing marker starts NULL — no live chain, so the next sweep gives it one —
and the downgrade drops the column without touching the markers.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database




@pytest.fixture
def at_0021():
    engine, cfg = migration_database("0021_library_job_runs")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
            "VALUES ('u1', 'u1', 'x', 'user', true, :now)"), {"now": now})
        conn.execute(sa.text(
            "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) "
            "VALUES ('t1', 'T', 'A', 'B', 0, :now)"), {"now": now})
        conn.execute(sa.text(
            "INSERT INTO pending_likes (id, user_id, track_id, sync_provider, created_at, attempts) "
            "VALUES ('p1', 'u1', 't1', 'sync', :now, 42)"), {"now": now})
    return engine, cfg


def test_existing_markers_start_without_a_live_chain(at_0021):
    engine, cfg = at_0021

    command.upgrade(cfg, "0022_pending_like_next_attempt")

    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT attempts, next_attempt_at FROM pending_likes WHERE id = 'p1'")).one()
        indexes = {ix["name"] for ix in sa.inspect(conn).get_indexes("pending_likes")}
    assert row == (42, None)
    assert "ix_pending_likes_next_attempt_at" in indexes


def test_the_downgrade_drops_only_the_column(at_0021):
    engine, cfg = at_0021
    command.upgrade(cfg, "0022_pending_like_next_attempt")

    command.downgrade(cfg, "0021_library_job_runs")

    with engine.connect() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("pending_likes")}
        count = conn.execute(sa.text("SELECT count(*) FROM pending_likes")).scalar()
    assert "next_attempt_at" not in columns
    assert count == 1

    command.upgrade(cfg, "head")  # and up again
