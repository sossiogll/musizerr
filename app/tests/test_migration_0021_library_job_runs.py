"""
Migration 0021: ``user_library_syncs`` (one row per provider, created only
when the worker started) becomes ``library_job_runs``. Each user's latest row
becomes a finished ``library_sync`` run (a row left pending/running by a dead
worker becomes ``failed``/``worker_lost``), the partial unique index allows
one active run per user and kind, and the downgrade gives the old table back.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database




def _sync_row(conn, user_id, provider, status, *, started, tracks=0, error=None):
    conn.execute(
        sa.text(
            "INSERT INTO user_library_syncs (id, user_id, provider, status, tracks_synced, error_message, "
            "started_at, completed_at) VALUES (:id, :u, :p, :s, :n, :e, :st, :st)"
        ),
        {"id": str(uuid.uuid4()), "u": user_id, "p": provider, "s": status, "n": tracks, "e": error, "st": started},
    )


@pytest.fixture
def at_0020():
    engine, cfg = migration_database("0020_favorites")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for user in ("done", "failed", "stuck", "none"):
            conn.execute(sa.text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :id, 'x', 'user', true, :now)"), {"id": user, "now": now})
        _sync_row(conn, "done", "one", "failed", started=now - timedelta(days=2), error="old")
        _sync_row(conn, "done", "one", "done", started=now - timedelta(hours=1), tracks=12)
        _sync_row(conn, "failed", "two", "failed", started=now, error="[Errno 111] Connection refused")
        _sync_row(conn, "stuck", "one", "running", started=now)
    return engine, cfg


def _runs(conn) -> dict[str, dict]:
    rows = conn.execute(sa.text(
        "SELECT user_id, kind, status, providers, result, error_code, error_detail, created_at, completed_at "
        "FROM library_job_runs"
    )).mappings().all()
    return {row["user_id"]: dict(row) for row in rows}


def test_each_users_latest_sync_becomes_a_finished_run(at_0020):
    engine, cfg = at_0020

    command.upgrade(cfg, "0021_library_job_runs")

    with engine.connect() as conn:
        runs = _runs(conn)
        tables = sa.inspect(conn).get_table_names()
    assert "user_library_syncs" not in tables
    assert set(runs) == {"done", "failed", "stuck"}
    assert {r["kind"] for r in runs.values()} == {"library_sync"}

    done = runs["done"]
    assert done["status"] == "completed" and done["error_code"] is None
    assert json.loads(done["result"]) == {"tracks_synced": 12}
    assert json.loads(done["providers"])[0]["provider"] == "one"
    assert done["created_at"] and done["completed_at"]

    assert runs["failed"]["status"] == "failed" and runs["failed"]["error_code"] == "library_sync_failed"
    assert runs["failed"]["error_detail"] == "[Errno 111] Connection refused"
    # A dead worker's row is not "running" forever.
    assert runs["stuck"]["status"] == "failed" and runs["stuck"]["error_code"] == "worker_lost"


def test_one_active_run_per_user_and_kind(at_0020):
    engine, cfg = at_0020
    command.upgrade(cfg, "0021_library_job_runs")
    insert = sa.text(
        "INSERT INTO library_job_runs (id, user_id, kind, status, processed, created_at) "
        "VALUES (:id, 'none', :kind, :status, 0, :now)"
    )
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        conn.execute(insert, {"id": "a", "kind": "library_sync", "status": "queued", "now": now})
        conn.execute(insert, {"id": "b", "kind": "download_sync", "status": "running", "now": now})
        conn.execute(insert, {"id": "c", "kind": "library_sync", "status": "completed", "now": now})
        conn.execute(insert, {"id": "d", "kind": "library_sync", "status": "failed", "now": now})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"id": "e", "kind": "library_sync", "status": "running", "now": now})


def test_the_downgrade_restores_the_old_table(at_0020):
    engine, cfg = at_0020
    command.upgrade(cfg, "0021_library_job_runs")

    command.downgrade(cfg, "0020_favorites")

    with engine.connect() as conn:
        tables = sa.inspect(conn).get_table_names()
        rows = conn.execute(sa.text(
            "SELECT user_id, provider, status, tracks_synced, error_message FROM user_library_syncs"
        )).all()
    assert "library_job_runs" not in tables
    by_user = {r[0]: r for r in rows}
    assert by_user["done"][1:4] == ("one", "done", 12)
    assert by_user["failed"][2] == "failed" and by_user["failed"][4]
    assert by_user["stuck"][2] == "failed"

    command.upgrade(cfg, "head")  # and up again
