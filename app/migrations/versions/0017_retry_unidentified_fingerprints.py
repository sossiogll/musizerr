"""forget cached fingerprints without an identity

Revision ID: 0017_retry_unidentified_fingerprints
Revises: 0016_drop_anonymous_downloads
Create Date: 2026-09-28

La cache dei fingerprint tiene l'identità grezza del provider e la soglia di
confidenza si applica a ogni lettura; un file non raggiunto non entra in
cache. Le righe senza identità scritte prima non distinguono un file non
riconosciuto da uno sotto soglia o non raggiunto: si cancellano, e il
prossimo scan le rifà. Il downgrade non ricrea nulla (è solo una cache).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_retry_unidentified_fingerprints"
down_revision: Union[str, None] = "0016_drop_anonymous_downloads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM duplicate_fingerprint_cache WHERE recording_id IS NULL"))


def downgrade() -> None:
    pass
