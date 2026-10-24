"""downloads: how long a delegation was really observed

Revision ID: 0028_delegation_observation
Revises: 0027_routine_run_queue_times
Create Date: 2026-10-03

A delegated download expired on the wall clock (``delegation_max_age_hours``
since the dispatch) even when its polls had been skipped (a queue busy with
downloads) or the external system had not answered: the first answer after
the max age failed it, not retriable. Now the reconciler counts the polls
the external system answered (``delegation_answers``), the last one
(``delegation_answered_at``) and the time those answers cover
(``delegation_observed_seconds``); the max age applies to that time.

The rows already delegated start from zero: their observation begins with
the next answered poll.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_delegation_observation"
down_revision: Union[str, None] = "0027_routine_run_queue_times"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds columns with a default


def upgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.add_column(sa.Column("delegation_answers", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("delegation_answered_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("delegation_observed_seconds", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.drop_column("delegation_observed_seconds")
        batch.drop_column("delegation_answered_at")
        batch.drop_column("delegation_answers")
