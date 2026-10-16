"""add user_saved_tracks and user_library_syncs tables

Revision ID: 0004
Revises: b5431e08b0b9
Create Date: 2026-06-18

Tables for syncing saved/liked tracks from metadata providers per-user.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "b5431e08b0b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_saved_tracks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("dedup_type", sa.String(), nullable=False),
        sa.Column("source_provider", sa.String(), nullable=False),
        sa.Column("track_data", sa.String(), nullable=False),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_saved_tracks_user_id", "user_saved_tracks", ["user_id"])
    op.create_index(
        "ix_ust_user_dedup",
        "user_saved_tracks",
        ["user_id", "dedup_key"],
        unique=True,
    )

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


def downgrade() -> None:
    op.drop_table("user_library_syncs")
    op.drop_table("user_saved_tracks")
