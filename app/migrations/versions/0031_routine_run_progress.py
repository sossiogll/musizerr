"""routine_runs: the progress of a run while it executes

Revision ID: 0031_routine_run_progress
Revises: 0030_unlike_on_origin_per_user
Create Date: 2026-10-03

A long routine (the first index of a large sync library) reports where it
is: ``progress`` holds the phase, the provider and processed/total as JSON,
``heartbeat_at`` the time of the last report. Both stay NULL for the
routines that do not report, and for the rows written before.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_routine_run_progress"
down_revision: Union[str, None] = "0030_unlike_on_origin_per_user"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds nullable columns


def upgrade() -> None:
    with op.batch_alter_table("routine_runs") as batch:
        batch.add_column(sa.Column("progress", sa.String(), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("routine_runs") as batch:
        batch.drop_column("heartbeat_at")
        batch.drop_column("progress")
