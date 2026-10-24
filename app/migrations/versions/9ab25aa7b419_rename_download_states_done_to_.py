"""rename_download_states_done_to_downloaded

Revision ID: 9ab25aa7b419
Revises: a7783f746af4
Create Date: 2026-09-10 00:31:40.625469

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9ab25aa7b419'
down_revision: Union[str, None] = 'a7783f746af4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # rewrites downloads.status values the previous code does not know


def upgrade() -> None:
    # Vocabolario di stato dei download unificato (core.download_states):
    #   done    -> downloaded (terminale OK)
    #   running -> downloading (fase di recupero audio)
    # Solo la tabella downloads; gli stati dei sync-row restano invariati.
    op.execute("UPDATE downloads SET status = 'downloaded' WHERE status = 'done'")
    op.execute("UPDATE downloads SET status = 'downloading' WHERE status = 'running'")


def downgrade() -> None:
    op.execute("UPDATE downloads SET status = 'done' WHERE status = 'downloaded'")
    op.execute("UPDATE downloads SET status = 'running' WHERE status = 'downloading'")
