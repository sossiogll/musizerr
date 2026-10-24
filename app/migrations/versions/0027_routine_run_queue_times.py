"""routine_runs: when a run was queued, when it really started

Revision ID: 0027_routine_run_queue_times
Revises: 0026_removed_favorites
Create Date: 2026-10-03

A run was written ``running`` with ``started_at`` = the dispatch time, so a
run waiting in a busy queue read "running" for hours and its duration was
mostly the wait. Now the dispatch writes ``queued_at`` and the row stays
``queued`` until the worker starts it, which writes ``started_at`` (NULL
until then, and for a run that never started).

Backfill: the rows written before keep their only known time in both
columns (``queued_at = started_at``).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_routine_run_queue_times"
down_revision: Union[str, None] = "0026_removed_favorites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # makes routine_runs.queued_at NOT NULL, changes the status default and loosens started_at


def upgrade() -> None:
    with op.batch_alter_table("routine_runs") as batch:
        batch.add_column(sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE routine_runs SET queued_at = started_at")
    with op.batch_alter_table("routine_runs") as batch:
        batch.alter_column("queued_at", existing_type=sa.DateTime(), nullable=False)
        batch.alter_column("started_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("status", existing_type=sa.String(), server_default="queued")
        batch.drop_index("ix_routine_runs_key_started")
        batch.create_index("ix_routine_runs_key_queued", ["routine_key", "queued_at"])


def downgrade() -> None:
    op.execute("UPDATE routine_runs SET started_at = queued_at WHERE started_at IS NULL")
    with op.batch_alter_table("routine_runs") as batch:
        batch.drop_index("ix_routine_runs_key_queued")
        batch.create_index("ix_routine_runs_key_started", ["routine_key", "started_at"])
        batch.alter_column("status", existing_type=sa.String(), server_default="running")
        batch.alter_column("started_at", existing_type=sa.DateTime(), nullable=False)
        batch.drop_column("queued_at")
