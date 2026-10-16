"""downloads: retriable failures, completed_at only on terminal states

Revision ID: 0023_download_retriable
Revises: 0022_pending_like_next_attempt
Create Date: 2026-09-29

``downloads.retriable`` (nullable): on a failed download, whether its last
failure was transient (network, upstream errors, timeouts: retried later by
the ``retry_transient_failures`` routine) or definitive (nobody has the
track). Rows failed before this revision stay NULL: unknown.

``completed_at`` is set iff the state is terminal (downloaded, failed). Data
fix for the rows written before: a dispatch set it (not terminal) and a
failure did not (terminal). A failed row without it takes the best time
known, its start.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_download_retriable"
down_revision: Union[str, None] = "0022_pending_like_next_attempt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TERMINAL = ("downloaded", "failed")


def upgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.add_column(sa.Column("retriable", sa.Boolean(), nullable=True))
    downloads = sa.table(
        "downloads",
        sa.column("status", sa.String),
        sa.column("started_at", sa.DateTime),
        sa.column("completed_at", sa.DateTime),
    )
    op.execute(
        downloads.update()
        .where(downloads.c.status.not_in(_TERMINAL), downloads.c.completed_at.is_not(None))
        .values(completed_at=None)
    )
    op.execute(
        downloads.update()
        .where(downloads.c.status.in_(_TERMINAL), downloads.c.completed_at.is_(None))
        .values(completed_at=sa.func.coalesce(downloads.c.started_at, sa.func.current_timestamp()))
    )


def downgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.drop_column("retriable")
