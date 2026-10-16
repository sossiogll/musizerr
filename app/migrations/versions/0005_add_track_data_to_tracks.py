"""add track_data JSON column to tracks table

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-19

Stores full serialized Track data so the downloads endpoint can return
a complete Track object without re-fetching from metadata providers.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.add_column(sa.Column("track_data", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.drop_column("track_data")
