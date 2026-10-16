"""add library_entries.storage_provider

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-18

The column was added to the SQLModel in commit 293da5f (StorageProvider
introduction) but the runtime relied on create_all() which never alters
existing tables. This migration backfills it for any DB that was created
before alembic was wired up.

Idempotent: some instances were live-patched with a manual ALTER TABLE,
so the column may already exist.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("library_entries", "storage_provider"):
        with op.batch_alter_table("library_entries") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "storage_provider",
                    sa.String(),
                    nullable=False,
                    server_default="local",
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("library_entries") as batch_op:
        batch_op.drop_column("storage_provider")
