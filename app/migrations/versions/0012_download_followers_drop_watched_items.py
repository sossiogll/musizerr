"""add download_followers, drop watched_items (never implemented)

Revision ID: 0012_download_followers_drop_watched_items
Revises: 0011_drop_user_setting_columns
Create Date: 2026-09-19

Due cambi indipendenti nello stesso giro di migrazione:

1. ``download_followers`` — chi si aggancia a una richiesta di download già
   in corso (``attach_to_open_download``, ``routes/content/_download.py``)
   non vedeva più quella richiesta nella propria cronologia (``GET
   /downloads``, ``routes/history.py`` filtra su ``Download.requested_by``,
   che resta il richiedente originale). Questa tabella registra gli
   agganci successivi: ``(download_id, user_id)`` unico, cosi' la
   cronologia può includere anche le righe a cui l'utente è agganciato
   senza toccare ``requested_by``.

2. ``watched_items`` — modello di una feature ("segui playlist/artista")
   mai implementata oltre lo schema (introdotta in ``0001_baseline``,
   nessuna route/worker l'ha mai letta o scritta). Il proprietario ha
   deciso di rimuoverla. Verificato prima di scrivere questa migrazione:
   la tabella è vuota nel DB live (``select count(*) from watched_items``
   → 0), quindi il drop non perde dati reali. ``downgrade()`` la ricrea
   identica a come l'aveva definita ``0001_baseline``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_download_followers_drop_watched_items"
down_revision: Union[str, None] = "0011_drop_user_setting_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # adds a table and drops the watched_items table


def upgrade() -> None:
    op.create_table(
        "download_followers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("download_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["download_id"], ["downloads.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("download_id", "user_id", name="uq_download_follower"),
    )
    op.create_index("ix_download_followers_download_id", "download_followers", ["download_id"])
    op.create_index("ix_download_followers_user_id", "download_followers", ["user_id"])

    op.drop_index("ix_watched_items_user_id", table_name="watched_items")
    op.drop_table("watched_items")


def downgrade() -> None:
    op.create_table(
        "watched_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("auto_download", sa.Boolean(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watched_items_user_id", "watched_items", ["user_id"])

    op.drop_index("ix_download_followers_user_id", table_name="download_followers")
    op.drop_index("ix_download_followers_download_id", table_name="download_followers")
    op.drop_table("download_followers")
