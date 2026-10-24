"""name the index on user_saved_track_providers.saved_track_id as the model does

Revision ID: 0034_saved_track_provider_index_name
Revises: 0033_file_sizes_bigint
Create Date: 2026-10-04

Migration 0006 named the index ``ix_ustp_saved_track_id``; the model declares
it with ``index=True``, which names it ``ix_user_saved_track_providers_saved_
track_id``. A database that went through later table rebuilds may already have
the model's name (and not the old one), so the migration looks at the schema
it finds: it drops the old index only if there is one and creates the new one
only if it is missing. The schema and the models now agree (``alembic check``
reports no difference).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_saved_track_provider_index_name"
down_revision: Union[str, None] = "0033_file_sizes_bigint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # renames an index no code names

_TABLE = "user_saved_track_providers"
_OLD = "ix_ustp_saved_track_id"
_NEW = "ix_user_saved_track_providers_saved_track_id"


def _rename(drop: str, create: str) -> None:
    present = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if drop in present:
        op.drop_index(drop, table_name=_TABLE)
    if create not in present:
        op.create_index(create, _TABLE, ["saved_track_id"])


def upgrade() -> None:
    _rename(drop=_OLD, create=_NEW)


def downgrade() -> None:
    _rename(drop=_NEW, create=_OLD)
