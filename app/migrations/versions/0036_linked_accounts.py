"""linked accounts: a disconnected account is a deleted row, not a marker

Revision ID: 0036_linked_accounts
Revises: 0035_download_requests
Create Date: 2026-10-10

Before, disconnecting an OAuth2 account wrote ``{"revoked": true}`` over its
token, so that the provider's app-only fallback would not log the user back in
silently. Which account a call uses is now decided by one rule, from the model
each provider declares (personal / personal with shared / system), and a user
who disconnected their own account simply has none: the call goes to the shared
account when the provider has one. Every marker is therefore deleted.

Nothing else moves, on purpose. The rows keep their meaning: ``user_id`` NULL is
the shared (instance) account, a user's id is that user's own. What changes is
who writes the shared one: an administrator, from Administration > Providers
(and only there). The copy the old code made of an administrator's own account
into the shared one stays as it is, so the shared accounts keep working the day
this migration runs; the two rows are now independent accounts, which an admin
can disconnect or replace on their own.

The downgrade is empty: a deleted marker carries nothing to restore.
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_linked_accounts"
down_revision: Union[str, None] = "0035_download_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # deletes the revoked-account marker rows; the downgrade does nothing
irreversible = True  # its downgrade cannot be relied on to restore the previous state

_CHUNK = 500


def _is_marker(raw: str) -> bool:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("revoked") is True


def upgrade() -> None:
    bind = op.get_bind()
    credentials = sa.table(
        "provider_credentials",
        sa.column("id", sa.String),
        sa.column("credential_type", sa.String),
        sa.column("data", sa.String),
    )
    rows = bind.execute(
        sa.select(credentials.c.id, credentials.c.data).where(credentials.c.credential_type == "oauth2_token")
    ).all()
    markers = [row.id for row in rows if _is_marker(row.data)]
    for start in range(0, len(markers), _CHUNK):
        bind.execute(sa.delete(credentials).where(credentials.c.id.in_(markers[start:start + _CHUNK])))


def downgrade() -> None:
    pass
