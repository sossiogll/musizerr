"""drop the allow_anonymous_downloads setting

Revision ID: 0016_drop_anonymous_downloads
Revises: 0015_local_storage_root
Create Date: 2026-09-27

I download richiedono sempre un utente autenticato quando l'autenticazione è
attiva: l'impostazione ``allow_anonymous_downloads`` non esiste più. Le righe
già salvate (di sistema o per utente) vengono cancellate. Il downgrade non
ricrea nulla: senza riga vale il default dell'impostazione, che era False.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_drop_anonymous_downloads"
down_revision: Union[str, None] = "0015_local_storage_root"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # deletes the allow_anonymous_downloads setting rows; the downgrade cannot restore them
irreversible = True  # its downgrade cannot be relied on to restore the previous state


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key = :key"),
        {"key": "allow_anonymous_downloads"},
    )


def downgrade() -> None:
    pass
