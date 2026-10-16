"""
Migrations 0027 (``routine_runs.queued_at``, ``started_at`` nullable) and
0028 (the delegation observation columns of ``downloads``): the rows written
before keep their only known time in both columns, and the delegations
already in flight start their observation from zero. Both downgrade.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

STARTED = datetime(2026, 10, 2, 15, 18)


@pytest.fixture
def db():
    engine, cfg = migration_database("0026_removed_favorites")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO routine_runs (id, routine_key, started_at, finished_at, status) "
            "VALUES ('r', 'match_likes', :s, :s, 'ok')"), {"s": STARTED})
        conn.execute(sa.text(
            "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) "
            "VALUES ('t', 'Her', 'A', 'B', 1, :s)"), {"s": STARTED})
        conn.execute(sa.text(
            "INSERT INTO downloads (id, track_id, status, retry_count, started_at, delegation_handle) "
            "VALUES ('d', 't', 'dispatched', 0, :s, '7687')"), {"s": STARTED})
    return engine, cfg


def test_upgrade_backfills_queued_at_and_starts_the_observation_from_zero(db):
    engine, cfg = db
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        queued, started = conn.execute(sa.text("SELECT queued_at, started_at FROM routine_runs")).one()
        assert queued == started
        conn.execute(sa.text(
            "INSERT INTO routine_runs (id, routine_key, queued_at) VALUES ('q', 'match_likes', :s)"), {"s": STARTED})
        assert conn.execute(sa.text("SELECT status, started_at FROM routine_runs WHERE id='q'")).one() == ("queued", None)
        answers, answered_at, observed = conn.execute(sa.text(
            "SELECT delegation_answers, delegation_answered_at, delegation_observed_seconds FROM downloads")).one()
        assert (answers, answered_at, observed) == (0, None, 0)
        names = {i["name"] for i in sa.inspect(conn).get_indexes("routine_runs")}
        assert "ix_routine_runs_key_queued" in names and "ix_routine_runs_key_started" not in names


def test_downgrade_restores_the_previous_schema(db):
    engine, cfg = db
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO routine_runs (id, routine_key, queued_at) VALUES ('q', 'match_likes', :s)"), {"s": STARTED})

    command.downgrade(cfg, "0026_removed_favorites")

    with engine.connect() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("routine_runs")}
        assert "queued_at" not in columns
        assert conn.execute(sa.text("SELECT started_at FROM routine_runs WHERE id='q'")).scalar() is not None
        assert "delegation_answers" not in {c["name"] for c in sa.inspect(conn).get_columns("downloads")}
