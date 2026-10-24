"""local storage root: downloads_dir becomes the local storage provider's root_dir

Revision ID: 0015_local_storage_root
Revises: 0014_library_duplicates
Create Date: 2026-09-27

ADR-0003: la radice della libreria locale è un'impostazione dello storage
locale (``provider.local.root_dir``), non più dell'host (``downloads_dir``).
Un valore già salvato viene copiato; la chiave dell'host viene cancellata.
Senza valore salvato, lo storage locale usa il proprio default.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_local_storage_root"
down_revision: Union[str, None] = "0014_library_duplicates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # moves a setting to another key and deletes the old one

_HOST_KEY = "downloads_dir"
_PROVIDER_KEY = "provider.local.root_dir"


def _get_system_value(conn, key: str):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    return None if row is None else json.loads(row[0])


def _upsert_system(conn, key: str, value) -> None:
    now = datetime.now(timezone.utc)
    existing = conn.execute(
        sa.text("SELECT id FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    if existing is not None:
        conn.execute(
            sa.text("UPDATE settings SET value = :value, updated_at = :updated_at WHERE id = :id"),
            {"value": json.dumps(value), "updated_at": now, "id": existing[0]},
        )
    else:
        conn.execute(
            sa.text(
                "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, :key, 'system', NULL, :value, :updated_at)"
            ),
            {"id": str(uuid.uuid4()), "key": key, "value": json.dumps(value), "updated_at": now},
        )


def _delete_system(conn, key: str) -> None:
    conn.execute(
        sa.text("DELETE FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    )


def _move(conn, source: str, target: str) -> None:
    value = _get_system_value(conn, source)
    if value is not None and _get_system_value(conn, target) is None:
        _upsert_system(conn, target, value)
    _delete_system(conn, source)


def upgrade() -> None:
    _move(op.get_bind(), _HOST_KEY, _PROVIDER_KEY)


def downgrade() -> None:
    _move(op.get_bind(), _PROVIDER_KEY, _HOST_KEY)
