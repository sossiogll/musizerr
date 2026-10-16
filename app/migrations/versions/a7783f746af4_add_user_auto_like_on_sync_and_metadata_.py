"""add user auto_like_on_sync and metadata_provider_priority

Revision ID: a7783f746af4
Revises: 0007
Create Date: 2026-06-21 01:40:49.642785

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a7783f746af4'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_like_on_sync', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('metadata_provider_priority', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('metadata_provider_priority')
        batch_op.drop_column('auto_like_on_sync')
