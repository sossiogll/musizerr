"""library job runs: one row per library sync / download sync, created queued

Revision ID: 0021_library_job_runs
Revises: 0020_favorites
Create Date: 2026-09-28

- ``library_job_runs``: one run of a user's library job (``kind``
  ``library_sync`` or ``download_sync``), created ``queued`` when it is
  requested and moved by the worker to ``running`` → ``completed``/``failed``,
  with generic progress (phase, provider, processed/total, per-provider
  entries) and a heartbeat. The partial unique index
  ``uq_library_job_active`` allows one queued or running run per user and
  kind.
- ``user_library_syncs`` (one row per provider, created only when the worker
  started) is replaced: each user's latest row becomes a finished
  ``library_sync`` run, so "last sync" survives; a row left ``pending`` or
  ``running`` by a dead worker becomes ``failed`` (``worker_lost``).

The downgrade recreates ``user_library_syncs`` with one row per provider of
every finished ``library_sync`` run.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_library_job_runs"
down_revision: Union[str, None] = "0020_favorites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FAILED_MESSAGE = "The library sync failed."
_LOST_MESSAGE = "The worker stopped before the job finished."


def upgrade() -> None:
    conn = op.get_bind()
    op.create_table(
        "library_job_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("params", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("providers", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("error_detail", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_library_job_runs_user_id", "library_job_runs", ["user_id"])
    op.create_index(
        "ix_library_job_runs_user_kind_created", "library_job_runs", ["user_id", "kind", "created_at"],
    )
    op.create_index(
        "uq_library_job_active", "library_job_runs", ["user_id", "kind"], unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    rows = conn.execute(sa.text(
        "SELECT user_id, provider, status, tracks_synced, error_message, started_at, completed_at FROM ("
        "SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY started_at DESC, id DESC) AS n "
        "FROM user_library_syncs) AS ranked WHERE n = 1"
    )).fetchall()
    for user_id, provider, status, tracks_synced, error_message, started_at, completed_at in rows:
        if status == "done":
            new_status, code, message = "completed", None, None
        elif status == "failed":
            new_status, code, message = "failed", "library_sync_failed", _FAILED_MESSAGE
        else:
            new_status, code, message = "failed", "worker_lost", _LOST_MESSAGE
        entry = {"provider": provider, "status": new_status, "processed": tracks_synced or 0,
                 "total": None, "count": tracks_synced or 0, "error_code": code, "error_message": message}
        conn.execute(
            sa.text(
                "INSERT INTO library_job_runs (id, user_id, kind, status, processed, providers, result, "
                "error_code, error_message, error_detail, created_at, started_at, completed_at) VALUES "
                "(:id, :user_id, 'library_sync', :status, 0, :providers, :result, :code, :message, :detail, "
                ":created_at, :started_at, :completed_at)"
            ),
            {
                "id": str(uuid.uuid4()), "user_id": user_id, "status": new_status,
                "providers": json.dumps([entry]), "result": json.dumps({"tracks_synced": tracks_synced or 0}),
                "code": code, "message": message, "detail": error_message if new_status == "failed" else None,
                "created_at": started_at or completed_at or datetime.now(timezone.utc), "started_at": started_at,
                "completed_at": completed_at or started_at,
            },
        )

    op.drop_index("ix_user_library_syncs_user_id", table_name="user_library_syncs")
    op.drop_table("user_library_syncs")


def downgrade() -> None:
    conn = op.get_bind()
    op.create_table(
        "user_library_syncs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("tracks_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_library_syncs_user_id", "user_library_syncs", ["user_id"])

    runs = conn.execute(sa.text(
        "SELECT user_id, status, providers, error_detail, error_message, started_at, completed_at "
        "FROM library_job_runs WHERE kind = 'library_sync' AND status IN ('completed', 'failed')"
    )).fetchall()
    for user_id, status, providers, detail, message, started_at, completed_at in runs:
        for entry in json.loads(providers or "[]"):
            ok = entry.get("status") == "completed"
            conn.execute(
                sa.text(
                    "INSERT INTO user_library_syncs (id, user_id, provider, status, tracks_synced, "
                    "error_message, started_at, completed_at) VALUES "
                    "(:id, :user_id, :provider, :status, :count, :error, :started_at, :completed_at)"
                ),
                {
                    "id": str(uuid.uuid4()), "user_id": user_id, "provider": entry.get("provider") or "",
                    "status": "done" if ok else "failed", "count": entry.get("count") or 0,
                    "error": None if ok else (entry.get("error_message") or detail or message),
                    "started_at": started_at, "completed_at": completed_at,
                },
            )

    op.drop_index("uq_library_job_active", table_name="library_job_runs")
    op.drop_index("ix_library_job_runs_user_kind_created", table_name="library_job_runs")
    op.drop_index("ix_library_job_runs_user_id", table_name="library_job_runs")
    op.drop_table("library_job_runs")
