"""schema_info: which app last migrated the database and which can still use it

Revision ID: 0043_schema_info
Revises: 0042_open_mode_accounts
Create Date: 2026-10-24

ADR-0007 section 4. A table of ONE row, written by ``init`` after every migration run
(``services/database_init.py``): the revisions the database is at, the version that migrated it, the oldest
version that can run on it and the newest contract migration applied. The start check of the API and the
workers reads it to tell an app that is merely older than the database (a rollback over expand-only
migrations: it starts) from one that cannot use it (a contract migration it does not have: it refuses).

A new table only, nothing to backfill: the row is written by ``init`` once this migration has run. The
downgrade drops it.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_schema_info"
down_revision: Union[str, None] = "0042_open_mode_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds a table


def upgrade() -> None:
    op.create_table(
        "schema_info",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("app_version", sa.String(), nullable=False),
        sa.Column("min_app_version", sa.String(), nullable=True),
        sa.Column("contract_revision", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=False, server_default="expand"),
        sa.Column("migrated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("schema_info")
