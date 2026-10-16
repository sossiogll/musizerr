"""provider login: the id of a linked account, pending accounts, one-time login codes

Revision ID: 0037_provider_login
Revises: 0036_linked_accounts
Create Date: 2026-10-10

Signing in through a provider a user has linked (``core/provider_login.py``):

- ``provider_credentials.external_id``: the stable id the provider gave the
  account (``musizerr_sdk.auth.AccountIdentity``), written when a user links an
  account of a provider that can sign users in. ``uq_provider_credential_
  external`` makes it unique per provider among the rows that carry one: an
  account of a provider is linked to ONE user.
- ``users.pending_approval``: an account a sign-in through a provider
  created while the admin's policy asks to approve new accounts. It is
  inactive until an admin activates it.
- ``provider_login_codes``: the one-time code the callback of a sign-in hands
  the browser to be exchanged for a session (only its hash is stored).

Nothing is backfilled: the accounts linked before this migration carry no id
(the provider was never asked who they are) and cannot sign anybody in until
their owner reconnects them from a signed-in session. The downgrade drops all
three.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_provider_login"
down_revision: Union[str, None] = "0036_linked_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HAS_EXTERNAL_ID = sa.text("external_id IS NOT NULL")


def upgrade() -> None:
    # A plain ADD COLUMN (SQLite has it): a batch copy-rename would have to carry the partial indexes along.
    op.add_column("provider_credentials", sa.Column("external_id", sa.String(), nullable=True))
    op.create_index(
        "uq_provider_credential_external", "provider_credentials", ["provider", "external_id"],
        unique=True, sqlite_where=_HAS_EXTERNAL_ID, postgresql_where=_HAS_EXTERNAL_ID,
    )

    op.add_column("users", sa.Column("pending_approval", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "provider_login_codes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_login_codes_user_id", "provider_login_codes", ["user_id"])
    op.create_index("ix_provider_login_codes_code_hash", "provider_login_codes", ["code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_provider_login_codes_code_hash", table_name="provider_login_codes")
    op.drop_index("ix_provider_login_codes_user_id", table_name="provider_login_codes")
    op.drop_table("provider_login_codes")
    op.drop_column("users", "pending_approval")
    op.drop_index("uq_provider_credential_external", table_name="provider_credentials")
    op.drop_column("provider_credentials", "external_id")
