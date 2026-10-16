"""drop the second index on duplicate_members.group_id

Revision ID: 0018_drop_duplicate_members_group_index
Revises: 0017_retry_unidentified_fingerprints
Create Date: 2026-09-28

``duplicate_members.group_id`` aveva due indici identici
(``ix_duplicate_members_group_id`` e ``ix_duplicate_members_group``): ogni
scrittura li aggiornava entrambi. Resta il primo, quello dichiarato dal
modello con ``index=True``.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0018_drop_duplicate_members_group_index"
down_revision: Union[str, None] = "0017_retry_unidentified_fingerprints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_duplicate_members_group", table_name="duplicate_members")


def downgrade() -> None:
    op.create_index("ix_duplicate_members_group", "duplicate_members", ["group_id"])
