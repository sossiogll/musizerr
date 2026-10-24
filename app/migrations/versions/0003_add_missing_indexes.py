"""add missing indexes on downloads and track_identifiers

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-18

Commit 3a8201e added index=True on Download.status, Download.requested_by
and a composite Index on TrackIdentifierRecord(id_type, value). Since the
tables already existed, create_all() never applied these.

Uses IF NOT EXISTS so this is safe on DBs where the indexes were already
created at table creation time (fresh installs after 3a8201e).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds indexes


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_downloads_status "
        "ON downloads (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_downloads_requested_by "
        "ON downloads (requested_by)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_track_identifier_type_value "
        "ON track_identifiers (id_type, value)"
    )


def downgrade() -> None:
    op.drop_index("ix_track_identifier_type_value", table_name="track_identifiers")
    op.drop_index("ix_downloads_requested_by", table_name="downloads")
    op.drop_index("ix_downloads_status", table_name="downloads")
