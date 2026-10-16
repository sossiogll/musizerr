"""pending likes: next_attempt_at, one reconcile chain per marker

Revision ID: 0022_pending_like_next_attempt
Revises: 0021_library_job_runs
Create Date: 2026-09-29

``pending_likes.next_attempt_at`` (nullable, indexed): when the marker's next
reconcile attempt is due. While it is in the future a run holds the marker or
is scheduled for it, and every other trigger for the same marker stops at
once; NULL means no chain is alive, so the periodic sweep resumes it.
Existing rows start NULL: the first sweep after the upgrade gives each one a
single chain.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_pending_like_next_attempt"
down_revision: Union[str, None] = "0021_library_job_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("pending_likes") as batch:
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
    op.create_index("ix_pending_likes_next_attempt_at", "pending_likes", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_likes_next_attempt_at", table_name="pending_likes")
    with op.batch_alter_table("pending_likes") as batch:
        batch.drop_column("next_attempt_at")
