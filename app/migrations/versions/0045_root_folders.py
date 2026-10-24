"""root_folders and host_state: the folders the administrator declares, and what the host remembers

Revision ID: 0045_root_folders
Revises: 0044_rename_duplicate_origin
Create Date: 2026-10-25

ADR-0008 (root folders and folder settings). Two new tables, nothing is backfilled and nothing the previous release reads
changes, so the previous release works on the result:

- ``root_folders``: a location the administrator allows the product to use (an id, a label, an absolute ``path`` as the
  containers see it, unique), and the gateway's last verdict on it (``state``, ``reason``, ``checked_at``). A root has no kind
  and no role: a folder setting (a root plus a relative path) gives it one. Fresh installs have none.
- ``host_state``: facts the host remembers about itself between runs, as JSON text under a key. The one-shot conversion of
  today's folder values keeps there what it found and what it did (``services/roots.py``, migration ``0046``).

The downgrade drops both.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_root_folders"
down_revision: Union[str, None] = "0044_rename_duplicate_origin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds two tables


def upgrade() -> None:
    op.create_table(
        "root_folders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="unchecked"),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_table(
        "host_state",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False, server_default="null"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("host_state")
    op.drop_table("root_folders")
