"""
Migration 0023: ``downloads.retriable`` (NULL for the rows failed before),
and ``completed_at`` set iff the state is terminal — a dispatched row loses
the one the old dispatch wrote, a failed row without one takes its start.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

STARTED = datetime(2026, 9, 7, 12, 0)
DONE = datetime(2026, 9, 7, 12, 1)



@pytest.fixture
def migrated():
    engine, cfg = migration_database("0022_pending_like_next_attempt")
    rows = {
        "dispatched": ("dispatched", STARTED, DONE),
        "failed_open": ("failed", STARTED, None),
        "failed_closed": ("failed", STARTED, DONE),
        "downloaded": ("downloaded", STARTED, DONE),
        "pending": ("pending", None, None),
    }
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) "
            "VALUES ('t', 'Song', 'Artist', 'Album', 1, :now)"), {"now": STARTED})
        for row_id, (status, started, completed) in rows.items():
            conn.execute(sa.text(
                "INSERT INTO downloads (id, track_id, status, retry_count, started_at, completed_at) "
                "VALUES (:id, 't', :s, 0, :st, :c)"), {"id": row_id, "s": status, "st": started, "c": completed})
    command.upgrade(cfg, "0023_download_retriable")
    return engine, cfg


def _rows(engine) -> dict[str, tuple]:
    with engine.connect() as conn:
        return {
            row[0]: row[1:]
            for row in conn.execute(sa.text("SELECT id, completed_at, retriable FROM downloads")).all()
        }


def test_completed_at_is_set_iff_the_state_is_terminal(migrated):
    engine, _ = migrated
    rows = _rows(engine)

    assert rows["dispatched"][0] is None
    assert rows["pending"][0] is None
    assert str(rows["failed_open"][0]).startswith("2026-09-07 12:00")
    assert str(rows["failed_closed"][0]).startswith("2026-09-07 12:01")
    assert str(rows["downloaded"][0]).startswith("2026-09-07 12:01")
    assert all(retriable is None for _, retriable in rows.values())


def test_downgrade_drops_the_column(migrated):
    engine, cfg = migrated
    command.downgrade(cfg, "0022_pending_like_next_attempt")

    with engine.connect() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("downloads")}
    assert "retriable" not in columns
