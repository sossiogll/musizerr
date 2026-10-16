"""add pending_likes (auto-like reconciliation)

Revision ID: 0009_pending_likes
Revises: 0008_delegation_handle
Create Date: 2026-09-11

Stato desiderato durevole per il reconciler auto-like: sostituisce lo scheduling
"racy" del like (task ETA in memoria) con un marcatore in DB che sopravvive ai
riavvii del worker e converge sia per download sincroni sia delegati.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_pending_likes"
down_revision: Union[str, None] = "0008_delegation_handle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_likes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("sync_provider", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "track_id", "sync_provider", name="uq_pending_like"),
    )
    op.create_index("ix_pending_likes_user_id", "pending_likes", ["user_id"])
    op.create_index("ix_pending_likes_track_id", "pending_likes", ["track_id"])
    op.create_index("ix_pending_likes_sync_provider", "pending_likes", ["sync_provider"])


def downgrade() -> None:
    op.drop_index("ix_pending_likes_sync_provider", table_name="pending_likes")
    op.drop_index("ix_pending_likes_track_id", table_name="pending_likes")
    op.drop_index("ix_pending_likes_user_id", table_name="pending_likes")
    op.drop_table("pending_likes")
