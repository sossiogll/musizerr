"""library_entries: a file the storage no longer holds

Revision ID: 0029_library_entry_missing
Revises: 0028_delegation_observation
Create Date: 2026-10-03

Library entries kept pointing at files the storage had lost (renamed or
moved by later naming fixes or by external tools): the track stayed "in
library" with full playback while ``/stream`` answered 404. The library file
reconciliation (``services/library_reconcile.py``) relocates such an entry
when it finds the file again, otherwise it sets ``missing_since``: the entry,
and the downloads that landed on its path, no longer put the track in the
library. Existing rows start as present.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_library_entry_missing"
down_revision: Union[str, None] = "0028_delegation_observation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("library_entries") as batch:
        batch.add_column(sa.Column("missing_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_entries") as batch:
        batch.drop_column("missing_since")
