"""duplicate_members.origin: the application's own files are marked with the new name

Revision ID: 0044_rename_duplicate_origin
Revises: 0043_schema_info
Create Date: 2026-10-25

ADR-0010 row 21. A duplicate member whose file this application put in the library carries the
origin ``musizerr`` (``db/duplicate_store.py``); the product is now called Musizeer and the code compares with
``musizeer``. The old code cannot read the new value (a ``musizerr`` file it finds marked ``musizeer`` is
neither deletable nor known to it), so this is a contract migration, released in the MAJOR of the rename.

Nothing else stored carries the name: the other rows that do (``installed_providers.image``,
``provider_sources``, the ``workspace_dir`` setting) are created again or converted by their own procedure
(ADR-0010 row 22), and the table, column and revision names never had it.

The downgrade writes the old value back.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_rename_duplicate_origin"
down_revision: Union[str, None] = "0043_schema_info"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # rewrites a stored value the previous code cannot read

OLD, NEW = "musizerr", "musizeer"


def _rewrite(old: str, new: str) -> None:
    op.get_bind().execute(
        sa.text("UPDATE duplicate_members SET origin = :new WHERE origin = :old"),
        {"old": old, "new": new},
    )


def upgrade() -> None:
    _rewrite(OLD, NEW)


def downgrade() -> None:
    _rewrite(NEW, OLD)
