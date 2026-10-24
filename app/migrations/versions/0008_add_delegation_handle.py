"""add delegation_handle to downloads

Revision ID: 0008_delegation_handle
Revises: 9ab25aa7b419
Create Date: 2026-09-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_delegation_handle"
down_revision: Union[str, None] = "9ab25aa7b419"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds a nullable column


def upgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.add_column(sa.Column("delegation_handle", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.drop_column("delegation_handle")
