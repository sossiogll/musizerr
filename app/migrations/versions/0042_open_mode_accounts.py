"""the accounts the instance's synthetic user connected are the shared ones

Revision ID: 0042_open_mode_accounts
Revises: 0041_encrypt_credentials
Create Date: 2026-10-23

With authentication off the instance has ONE owner, the synthetic user (``__system__``), and its
own account IS the instance's shared account (``services/credentials.py``): an account it
connects is stored as the shared one (``user_id`` NULL), read from there and listed once.

Before this change the linked-accounts flow could still write the synthetic user's own row. Such a
row would now be orphaned (nobody reads ``user_id = '__system__'`` any more), so the upgrade moves
it to the shared account: a login or an OAuth2 token of the synthetic user becomes the shared one
(when the instance also had a shared one for the same provider, the most recently written of the
two wins, the older is dropped), and loses its ``external_id`` (the id a person signs in with: a
shared account has none). Credentials of any other kind are never the synthetic user's and are not
touched. An instance that never had such a row (the usual one) changes nothing; the data is moved
as it is stored, sealed or not, so it needs no key.

The downgrade does not move them back (the old code reads a shared account for the synthetic
user too, through ``PERSONAL_WITH_SHARED``).
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_open_mode_accounts"
down_revision: Union[str, None] = "0041_encrypt_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

SYSTEM_USER_ID = "__system__"  # when this migration was written (db.models.SYSTEM_USER_ID)
PERSONAL_TYPES = ("oauth2_token", "credentials")


def upgrade() -> None:
    conn = op.get_bind()
    legacy = conn.execute(
        sa.text(
            "SELECT id, provider, credential_type, updated_at FROM provider_credentials "
            "WHERE user_id = :system AND credential_type IN :types"
        ).bindparams(sa.bindparam("types", expanding=True)),
        {"system": SYSTEM_USER_ID, "types": list(PERSONAL_TYPES)},
    ).all()
    moved = dropped = 0
    for row_id, provider, credential_type, updated_at in legacy:
        shared = conn.execute(
            sa.text(
                "SELECT id, updated_at FROM provider_credentials "
                "WHERE user_id IS NULL AND provider = :provider AND credential_type = :type"
            ),
            {"provider": provider, "type": credential_type},
        ).first()
        if shared is not None:
            if shared[1] is not None and updated_at is not None and shared[1] >= updated_at:
                conn.execute(sa.text("DELETE FROM provider_credentials WHERE id = :id"), {"id": row_id})
                dropped += 1
                continue
            conn.execute(sa.text("DELETE FROM provider_credentials WHERE id = :id"), {"id": shared[0]})
            dropped += 1
        conn.execute(
            sa.text("UPDATE provider_credentials SET user_id = NULL, external_id = NULL WHERE id = :id"),
            {"id": row_id},
        )
        moved += 1
    logger.info("%s: %d account(s) of the synthetic user made shared, %d superseded one(s) dropped.", revision, moved, dropped)


def downgrade() -> None:
    pass
