"""encrypt the stored credentials at rest

Revision ID: 0041_encrypt_credentials
Revises: 0040_storage_provider_explicit
Create Date: 2026-10-16

Until now the credentials were stored in clear: ``provider_credentials.data`` (OAuth2 tokens,
logins, API keys, OAuth clients, the accounts of a capture utility), ``provider_sources.credential``
(the token of a private catalog and its images) and the value of the secret settings of the catalog
(``delegation_webhook_token``). From here they are SEALED (``core/secret_box.py``: ``enc:v1:`` and a
Fernet token) with ``CREDENTIALS_KEY`` (docs/credentials.md).

The upgrade needs the key in the environment and REFUSES to run without it (or with a malformed
one), before it changes a row. It seals every value still in clear, and leaves alone the ones
already sealed (it can be run twice, and a database that was upgraded, downgraded and upgraded
again is not sealed twice) after checking that the configured keys open them: a value sealed with a
key that is not configured would be unreadable for good, so the upgrade stops with a count, and
nothing is written, rather than carry on.

Which settings are secret is declared by the settings themselves (``SettingDef.secret``). The
catalog's are listed below, as they are today (a migration does not follow the code that comes
after it); the ones a PROVIDER declares are known only to the registry, which seals them when it
loads (``services/secret_settings.py``).

The downgrade opens every sealed value back to clear text (the old code reads no other), with the
same key; it refuses, writing nothing, when some value cannot be opened. Both directions report
counts, never a value.
"""
from __future__ import annotations

import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from core import secret_box
from db import credential_cipher

revision: str = "0041_encrypt_credentials"
down_revision: Union[str, None] = "0040_storage_provider_explicit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # seals the stored credentials, a form the previous code cannot read; the downgrade needs the key and may refuse
irreversible = True  # its downgrade cannot be relied on to restore the previous state

logger = logging.getLogger("alembic.runtime.migration")

# The secret settings of the catalog (``core.settings_catalog``) when this migration was written.
CATALOG_SECRET_SETTINGS = ("delegation_webhook_token",)


def _box() -> secret_box.SecretBox:
    try:
        return credential_cipher.box()
    except credential_cipher.CredentialsKeyError as refused:
        raise RuntimeError(
            f"Migration {revision} encrypts the stored credentials and cannot run: {refused}"
        ) from None


def _rows(conn) -> list[tuple[str, str, str, str | None]]:
    """(table, id, column text, setting key) for everything this migration may rewrite, in clear or
    sealed alike: the text the sealed form wraps is the column's own (a JSON document for a
    setting)."""
    rows: list[tuple[str, str, str, str | None]] = []
    for row_id, data in conn.execute(sa.text("SELECT id, data FROM provider_credentials")):
        rows.append(("provider_credentials", row_id, data, None))
    for row_id, credential in conn.execute(sa.text("SELECT id, credential FROM provider_sources WHERE credential IS NOT NULL")):
        rows.append(("provider_sources", row_id, credential, None))
    secret_keys = sa.text("SELECT id, key, value FROM settings WHERE key IN :keys").bindparams(
        sa.bindparam("keys", expanding=True)
    )
    for row_id, key, value in conn.execute(secret_keys, {"keys": list(CATALOG_SECRET_SETTINGS)}):
        rows.append(("settings", row_id, value, key))
    return rows


_COLUMN = {"provider_credentials": "data", "provider_sources": "credential", "settings": "value"}


def _token_of(table: str, stored: str) -> str | None:
    """The sealed token a stored text holds, None when it is in clear. A setting keeps it inside
    an envelope of its JSON; the other two columns are the token itself."""
    if table == "settings":
        try:
            return secret_box.enveloped_token(json.loads(stored))
        except ValueError:
            return None
    return stored if secret_box.is_sealed(stored) else None


def _write(conn, table: str, row_id: str, old: str, new: str) -> None:
    column = _COLUMN[table]
    # ``AND column = :old``: a writer that changed the row meanwhile is not overwritten.
    conn.execute(
        sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id AND {column} = :old"),
        {"new": new, "id": row_id, "old": old},
    )


def upgrade() -> None:
    box = _box()
    conn = op.get_bind()
    to_seal: list[tuple[str, str, str]] = []
    already = foreign = 0
    for table, row_id, stored, _key in _rows(conn):
        token = _token_of(table, stored)
        if token is None:
            to_seal.append((table, row_id, stored))
            continue
        already += 1
        try:
            box.open(token)
        except secret_box.Unreadable:
            foreign += 1
    if foreign:
        raise RuntimeError(
            f"Migration {revision}: {foreign} stored credential(s) are already encrypted with a key that is not "
            f"{credential_cipher.ENV_KEY} or one of {credential_cipher.ENV_PREVIOUS}. Nothing was changed. Set the key "
            "they were encrypted with (and the current one as the first) and run it again."
        )
    for table, row_id, stored in to_seal:
        sealed = box.seal(stored)
        _write(conn, table, row_id, stored, json.dumps(secret_box.envelope(sealed)) if table == "settings" else sealed)
    logger.info("%s: %d stored credential(s) encrypted, %d were already.", revision, len(to_seal), already)


def downgrade() -> None:
    box = _box()
    conn = op.get_bind()
    opened: list[tuple[str, str, str, str]] = []
    unreadable = 0
    for table in ("provider_credentials", "provider_sources"):
        column = _COLUMN[table]
        where = " WHERE credential IS NOT NULL" if table == "provider_sources" else ""
        for row_id, stored in conn.execute(sa.text(f"SELECT id, {column} FROM {table}{where}")):
            token = _token_of(table, stored)
            if token is None:
                continue
            try:
                opened.append((table, row_id, stored, box.open(token)))
            except secret_box.Unreadable:
                unreadable += 1
    # Every settings row that holds an envelope (the catalog's and the ones providers declared).
    for row_id, value in conn.execute(sa.text("SELECT id, value FROM settings")):
        token = _token_of("settings", value)
        if token is None:
            continue
        try:
            opened.append(("settings", row_id, value, box.open(token)))
        except secret_box.Unreadable:
            unreadable += 1
    if unreadable:
        raise RuntimeError(
            f"Migration {revision} (downgrade): {unreadable} stored credential(s) cannot be opened with the configured "
            f"keys ({credential_cipher.ENV_KEY}, {credential_cipher.ENV_PREVIOUS}), and the earlier version reads "
            "nothing but clear text. Nothing was changed. Set the key they were encrypted with and run it again."
        )
    for table, row_id, stored, plaintext in opened:
        _write(conn, table, row_id, stored, plaintext)
    logger.info("%s (downgrade): %d stored credential(s) written back in clear.", revision, len(opened))
