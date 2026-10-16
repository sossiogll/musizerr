"""refresh_tokens: family and successor of each rotation

Revision ID: 0024_refresh_token_rotation
Revises: 0023_download_retriable
Create Date: 2026-09-29

``family_id``: the sign-in a token descends from (NULL on the tokens issued
before: each is its own family). ``replaced_by_id``: set when the token was
rotated, so a replay within the grace window answers the same successor and
one outside it revokes the family (``services/refresh_tokens.py``).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_refresh_token_rotation"
down_revision: Union[str, None] = "0023_download_retriable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.add_column(sa.Column("family_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("replaced_by_id", sa.String(), nullable=True))
        batch.create_index("ix_refresh_tokens_family_id", ["family_id"])


def downgrade() -> None:
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.drop_index("ix_refresh_tokens_family_id")
        batch.drop_column("replaced_by_id")
        batch.drop_column("family_id")
