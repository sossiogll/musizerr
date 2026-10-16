"""unlike_on_origin_providers becomes a personal setting

Revision ID: 0030_unlike_on_origin_per_user
Revises: 0029_library_entry_missing
Create Date: 2026-10-03

Taking a like back on the provider it came from acts on the account of the
user who removes the favorite, so it is now that user's own choice (scope
'user'), off until they turn it on. The global value an admin may have saved
does not become a default for everyone: users who never chose keep it off.
When it was on, each admin keeps it on as their own choice (only an admin
could turn it on, and it already acted on their accounts); the global row
goes either way. The downgrade turns the global value back on if any user
had it on, and drops the personal rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0030_unlike_on_origin_per_user"
down_revision: Union[str, None] = "0029_library_entry_missing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "unlike_on_origin_providers"


def _insert(conn, scope: str, user_id: str | None, value) -> None:
    conn.execute(
        sa.text("INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, :key, :scope, :user_id, :value, :updated_at)"),
        {"id": str(uuid4()), "key": KEY, "scope": scope, "user_id": user_id, "value": json.dumps(value),
         # Naive UTC, as db.types.UTCDateTime stores it.
         "updated_at": datetime.now(timezone.utc).replace(tzinfo=None)},
    )


def upgrade() -> None:
    conn = op.get_bind()
    global_value = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'system'"), {"key": KEY},
    ).scalar()
    if global_value is not None and json.loads(global_value) is True:
        admins = conn.execute(sa.text("SELECT id FROM users WHERE role = 'admin'")).scalars().all()
        chosen = set(conn.execute(
            sa.text("SELECT user_id FROM settings WHERE key = :key AND scope = 'user'"), {"key": KEY},
        ).scalars().all())
        for admin_id in admins:
            if admin_id not in chosen:
                _insert(conn, "user", admin_id, True)
    conn.execute(sa.text("DELETE FROM settings WHERE key = :key AND scope = 'system'"), {"key": KEY})


def downgrade() -> None:
    conn = op.get_bind()
    personal = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'user'"), {"key": KEY},
    ).scalars().all()
    conn.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": KEY})
    if any(json.loads(value) is True for value in personal):
        _insert(conn, "system", None, True)
