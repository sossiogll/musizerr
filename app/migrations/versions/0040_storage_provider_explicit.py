"""storage_provider is written, no longer defaulted

Revision ID: 0040_storage_provider_explicit
Revises: 0039_provider_sources_v2
Create Date: 2026-10-16

The settings catalog used to declare ``"local"`` as the default of
``storage_provider``: a provider name in the core, and the one exception the
Golden Rule (CLAUDE.md) put up with. The setting now has no default, so an
install that never wrote it (and relied on the default) would lose its
storage. This migration writes the old default as the stored system value
where, and only where, no system row exists yet: a value somebody chose (even
another provider) is never touched, and running it again changes nothing.

On a database that has just been created the value is written too, which is
what a fresh install did before: the choice of the storage stays visible in
the settings and can be changed there.

The downgrade does nothing: the previous code reads the same default when the
row is missing, and the row, if kept, says the same thing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0040_storage_provider_explicit"
down_revision: Union[str, None] = "0039_provider_sources_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "storage_provider"
PREVIOUS_DEFAULT = "local"


def upgrade() -> None:
    conn = op.get_bind()
    present = conn.execute(
        sa.text("SELECT 1 FROM settings WHERE key = :key AND scope = 'system'"), {"key": KEY},
    ).first()
    if present is not None:
        return
    conn.execute(
        sa.text("INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, :key, 'system', NULL, :value, :updated_at)"),
        {"id": str(uuid4()), "key": KEY, "value": json.dumps(PREVIOUS_DEFAULT),
         # Naive UTC, as db.types.UTCDateTime stores it.
         "updated_at": datetime.now(timezone.utc).replace(tzinfo=None)},
    )


def downgrade() -> None:
    pass
