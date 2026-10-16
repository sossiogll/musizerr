"""system routines: routine_runs table + migrate old beat intervals

Revision ID: 0013_routine_scheduler
Revises: 0012_download_followers_drop_watched_items
Create Date: 2026-09-19

Due cambi: 1) nuova tabella ``routine_runs`` (cronologia di esecuzione dei
"system routines", vedi workers/routines.py), nessun dato preesistente. 2)
``library_sync_interval_hours``/``delegation_sweep_interval_minutes`` sono
sostituiti da ``routine.<key>.interval_minutes`` (sempre minuti) — un valore
già salvato viene copiato (ore->minuti dove serve) e la chiave vecchia
cancellata; se non c'era nulla da migrare la nuova chiave usa il default.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_routine_scheduler"
down_revision: Union[str, None] = "0012_download_followers_drop_watched_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old_key, new_key, converter ore->minuti se serve)
_OLD_TO_NEW = (
    ("library_sync_interval_hours", "routine.sync_saved_tracks.interval_minutes", lambda hours: hours * 60),
    ("delegation_sweep_interval_minutes", "routine.sweep_delegations.interval_minutes", lambda minutes: minutes),
)


def _get_system_value(conn, key: str):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    if row is None:
        return None
    return json.loads(row[0])


def _upsert_system(conn, key: str, value) -> None:
    now = datetime.now(timezone.utc)
    existing = conn.execute(
        sa.text("SELECT id FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    if existing is not None:
        conn.execute(
            sa.text("UPDATE settings SET value = :value, updated_at = :updated_at WHERE id = :id"),
            {"value": json.dumps(value), "updated_at": now, "id": existing[0]},
        )
    else:
        conn.execute(
            sa.text(
                "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, :key, 'system', NULL, :value, :updated_at)"
            ),
            {"id": str(uuid.uuid4()), "key": key, "value": json.dumps(value), "updated_at": now},
        )


def _delete_system(conn, key: str) -> None:
    conn.execute(
        sa.text("DELETE FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        "routine_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("routine_key", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("detail", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_routine_runs_routine_key", "routine_runs", ["routine_key"])
    op.create_index("ix_routine_runs_key_started", "routine_runs", ["routine_key", "started_at"])

    for old_key, new_key, convert in _OLD_TO_NEW:
        old_value = _get_system_value(conn, old_key)
        if old_value is not None:
            _upsert_system(conn, new_key, convert(old_value))
        _delete_system(conn, old_key)


def downgrade() -> None:
    conn = op.get_bind()

    _reverse = {
        "routine.sync_saved_tracks.interval_minutes": ("library_sync_interval_hours", lambda minutes: max(1, minutes // 60)),
        "routine.sweep_delegations.interval_minutes": ("delegation_sweep_interval_minutes", lambda minutes: minutes),
    }
    for new_key, (old_key, convert) in _reverse.items():
        new_value = _get_system_value(conn, new_key)
        if new_value is not None:
            _upsert_system(conn, old_key, convert(new_value))
        _delete_system(conn, new_key)

    op.drop_index("ix_routine_runs_key_started", table_name="routine_runs")
    op.drop_index("ix_routine_runs_routine_key", table_name="routine_runs")
    op.drop_table("routine_runs")
