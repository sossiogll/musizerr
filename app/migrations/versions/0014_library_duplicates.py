"""library duplicates: duplicate_groups + duplicate_members + fingerprint cache

Revision ID: 0014_library_duplicates
Revises: 0013_routine_scheduler
Create Date: 2026-09-19

Tre tabelle nuove per il dedup di libreria (vedi CLAUDE.md, core/duplicates.py,
workers/duplicates.py, routes/library_duplicates.py): nessun dato preesistente
da migrare, solo schema nuovo.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_library_duplicates"
down_revision: Union[str, None] = "0013_routine_scheduler"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds tables


def upgrade() -> None:
    op.create_table(
        "duplicate_groups",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("recording_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_paths", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_duplicate_groups_recording_id", "duplicate_groups", ["recording_id"])
    op.create_index("ix_duplicate_groups_status", "duplicate_groups", ["status"])
    op.create_index(
        "ix_duplicate_groups_recording_status", "duplicate_groups", ["recording_id", "status"],
    )

    op.create_table(
        "duplicate_members",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("sync_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("album", sa.String(), nullable=False, server_default=""),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suffix", sa.String(), nullable=False, server_default=""),
        sa.Column("bitrate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lossless", sa.Boolean(), nullable=True),
        sa.Column("quality_known", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fingerprint_score", sa.Float(), nullable=True),
        sa.Column("origin", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("download_id", sa.String(), nullable=True),
        sa.Column("is_keeper", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["duplicate_groups.id"]),
        sa.ForeignKeyConstraint(["download_id"], ["downloads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_duplicate_members_group_id", "duplicate_members", ["group_id"])
    op.create_index("ix_duplicate_members_path", "duplicate_members", ["path"])
    op.create_index("ix_duplicate_members_group", "duplicate_members", ["group_id"])

    op.create_table(
        "duplicate_fingerprint_cache",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recording_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path", "size", name="uq_duplicate_fingerprint_path_size"),
    )
    op.create_index("ix_duplicate_fingerprint_cache_path", "duplicate_fingerprint_cache", ["path"])


def downgrade() -> None:
    op.drop_index("ix_duplicate_fingerprint_cache_path", table_name="duplicate_fingerprint_cache")
    op.drop_table("duplicate_fingerprint_cache")

    op.drop_index("ix_duplicate_members_group", table_name="duplicate_members")
    op.drop_index("ix_duplicate_members_path", table_name="duplicate_members")
    op.drop_index("ix_duplicate_members_group_id", table_name="duplicate_members")
    op.drop_table("duplicate_members")

    op.drop_index("ix_duplicate_groups_recording_status", table_name="duplicate_groups")
    op.drop_index("ix_duplicate_groups_status", table_name="duplicate_groups")
    op.drop_index("ix_duplicate_groups_recording_id", table_name="duplicate_groups")
    op.drop_table("duplicate_groups")
