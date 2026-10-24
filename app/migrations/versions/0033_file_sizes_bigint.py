"""file sizes are 64-bit

Revision ID: 0033_file_sizes_bigint
Revises: 0032_concurrency_constraints
Create Date: 2026-10-04

``duplicate_members.size`` and ``duplicate_fingerprint_cache.size`` hold a
file size in bytes. SQLite stores any integer in 64 bits, whatever the column
says; PostgreSQL's ``INTEGER`` stops at 2 GiB, which a long lossless file can
pass. The columns become ``BIGINT``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_file_sizes_bigint"
down_revision: Union[str, None] = "0032_concurrency_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # widens two columns from INTEGER to BIGINT

_TABLES = ("duplicate_members", "duplicate_fingerprint_cache")


def _retype(old: sa.types.TypeEngine, new: sa.types.TypeEngine) -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "size", existing_type=old, type_=new, existing_nullable=False, existing_server_default="0",
            )


def upgrade() -> None:
    _retype(sa.Integer(), sa.BigInteger())


def downgrade() -> None:
    _retype(sa.BigInteger(), sa.Integer())
