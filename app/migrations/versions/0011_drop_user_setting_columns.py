"""drop `users.auto_like_on_sync` and `users.metadata_provider_priority`
(Phase 4b: the two columns survive from before the unified `settings` store)

Revision ID: 0011_drop_user_setting_columns
Revises: 0010_settings_store
Create Date: 2026-09-17

Migration ``0010`` introduced the unified ``settings`` table and copied both
per-user columns into it (``auto_like_on_sync`` -> one ``user`` row per user
always; ``metadata_provider_priority`` -> one ``user`` row only where set),
but left the columns themselves in place — dropping them was explicitly
deferred to this phase, once every production reader/writer moved onto
``SettingsRepository``. That move happened alongside this migration (see
``app/routes/users_auth.py``, ``app/workers/tasks.py``,
``app/workers/maintenance.py``): ``auto_like_on_sync`` is now a declared
``SettingDef`` (scope='user', ``core/settings_catalog.py``), which is what
makes dropping the column safe. ``metadata_provider_priority`` had no
production reader left even before this migration (only tests and one
explanatory comment referenced the column); it was already a declared
``SettingDef`` (scope='system_with_user_override') since Phase 3b.

Idempotency / safety net: ``0010`` copied both columns exactly once, at the
time it ran. A user registered, or whose ``auto_like_on_sync``/
``metadata_provider_priority`` column value changed, in the window between
``0010`` running and this migration running would NOT be reflected in
``settings`` — that data would be silently lost the moment the columns are
dropped. ``upgrade()`` therefore re-scans every user row and inserts a
``settings`` row for any (key, user) pair that doesn't already have one,
using the exact same copy rules as ``0010`` (``auto_like_on_sync`` always
gets a row; ``metadata_provider_priority`` only when set to a decodable
JSON value). It never overwrites a ``settings`` row that already exists —
once a value lives in ``settings``, that row is the source of truth, and a
second run of this migration (e.g. re-run outside alembic's normal
bookkeeping) finds nothing left uncovered and inserts nothing. This makes
the pair (0010, 0011) idempotent and safe regardless of how many times
either runs, in either order relative to each other's data.

``render_as_batch`` (see ``env.py``) makes the column drops SQLite-safe
(copy-rename, since SQLite has no native ``DROP COLUMN`` before 3.35 and
SQLAlchemy's batch mode is used unconditionally here for consistency with
every other ALTER in this project).

``downgrade()`` recreates both columns (same types/defaults as the original
``a7783f746af4`` migration that first added them) and repopulates them from
the current ``settings`` rows — not from any snapshot, since none is kept.
This is exact for every user who has a ``settings`` row (i.e. everyone,
for ``auto_like_on_sync``); a user with no ``metadata_provider_priority``
row simply gets the column's default (``NULL``), which is the correct
"never overridden" state.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0011_drop_user_setting_columns"
down_revision: Union[str, None] = "0010_settings_store"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUTO_LIKE_KEY = "auto_like_on_sync"
_PRIORITY_KEY = "metadata_provider_priority"


def _ensure_user_setting_row(conn, key: str, user_id: str, value_json: str, now: datetime) -> None:
    """Inserisce una riga scope='user' per (key, user_id) SOLO se non esiste
    già — mai un blind insert, mai un overwrite di una riga già presente
    (che potrebbe essere più recente di quella derivabile dalla colonna)."""
    exists = conn.execute(
        sa.text("SELECT 1 FROM settings WHERE key = :key AND scope = 'user' AND user_id = :user_id"),
        {"key": key, "user_id": user_id},
    ).first()
    if exists is not None:
        return
    conn.execute(
        sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, 'user', :user_id, :value, :updated_at)"
        ),
        {"id": str(uuid.uuid4()), "key": key, "user_id": user_id, "value": value_json, "updated_at": now},
    )


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(timezone.utc)

    # Rete di sicurezza (vedi docstring del modulo): copre qualunque utente
    # creato o modificato fra l'esecuzione di 0010 e questa, prima del drop.
    user_rows = conn.execute(
        sa.text(f"SELECT id, {_PRIORITY_KEY}, {_AUTO_LIKE_KEY} FROM users")
    ).fetchall()
    for user_id, metadata_priority_json, auto_like_on_sync in user_rows:
        _ensure_user_setting_row(conn, _AUTO_LIKE_KEY, user_id, json.dumps(bool(auto_like_on_sync)), now)
        if metadata_priority_json:
            try:
                priority = json.loads(metadata_priority_json)
            except json.JSONDecodeError:
                priority = None
            if priority is not None:
                _ensure_user_setting_row(conn, _PRIORITY_KEY, user_id, json.dumps(priority), now)

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column(_PRIORITY_KEY)
        batch_op.drop_column(_AUTO_LIKE_KEY)


def downgrade() -> None:
    conn = op.get_bind()

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column(_AUTO_LIKE_KEY, sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column(_PRIORITY_KEY, sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    rows = conn.execute(
        sa.text(
            "SELECT user_id, key, value FROM settings "
            "WHERE scope = 'user' AND key IN (:auto_like_key, :priority_key)"
        ),
        {"auto_like_key": _AUTO_LIKE_KEY, "priority_key": _PRIORITY_KEY},
    ).fetchall()

    for user_id, key, value_json in rows:
        if user_id is None:
            continue
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError:
            continue
        if key == _AUTO_LIKE_KEY:
            conn.execute(
                sa.text(f"UPDATE users SET {_AUTO_LIKE_KEY} = :v WHERE id = :uid"),
                {"v": bool(value), "uid": user_id},
            )
        elif key == _PRIORITY_KEY:
            conn.execute(
                sa.text(f"UPDATE users SET {_PRIORITY_KEY} = :v WHERE id = :uid"),
                {"v": json.dumps(value), "uid": user_id},
            )
