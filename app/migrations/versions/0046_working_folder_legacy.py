"""the stored working folder (workspace_dir) is set aside for the one-shot conversion into a root folder plus a path

Revision ID: 0046_working_folder_legacy
Revises: 0045_root_folders
Create Date: 2026-10-25

ADR-0008. The setting ``workspace_dir`` held an absolute path as text. It is now a ``folder`` setting: a root folder and a
path inside it (``{"root_id", "path"}``), with NO default. The previous release reads the old shape and the new one cannot, so
this is a contract migration, released in the MAJOR.

What a database can know on its own is only what is stored. Whether a path can be expressed as a root plus a path depends on
what is mounted, which only the gateway sees, so the conversion itself runs at run time, once, when the gateway first answers
(``services/roots.py``). This migration only makes that possible and removes the old value from where the new code reads:

- if the database has been USED (an installed provider, a download, a library entry or a track), the old working folder is
  recorded in ``host_state`` under ``legacy_workspace_dir`` as ``{"value": <the stored path, or null>, "default": <the old
  default>, "old_leaf": <the hidden name the old default ended with>}``: the stored value when there was one, the old default
  otherwise (an upgraded install worked in it). A fresh install has nothing to convert and records nothing;
- the stored ``workspace_dir`` row is deleted, so the new code finds the setting unset (downloads are blocked until the
  conversion, or the administrator, sets it) rather than a text it cannot read.

The old file names under the old working folder are left on disk; the held uploads in it are dropped by the conversion (those
downloads repeat). The downgrade writes the stored value (or the old default) back and removes the record.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_working_folder_legacy"
down_revision: Union[str, None] = "0045_root_folders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # removes a stored value the previous code reads, in a shape it cannot read back

KEY = "legacy_workspace_dir"
OLD_LEAF = ".musiz" + "err-work"  # the hidden name the old default working folder ended with (the old product name)
OLD_DEFAULT = "/downloads/" + OLD_LEAF
USED_TABLES = ("installed_providers", "downloads", "library_entries", "tracks")


def _used(conn) -> bool:
    inspector = sa.inspect(conn)
    present = set(inspector.get_table_names())
    for table in USED_TABLES:
        if table in present and conn.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            return True
    return False


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text(
        "SELECT value FROM settings WHERE key = 'workspace_dir' AND scope = 'system' AND user_id IS NULL"
    )).first()
    stored = None
    if row is not None:
        try:
            decoded = json.loads(row[0])
        except ValueError:
            decoded = None
        stored = decoded if isinstance(decoded, str) and decoded.strip() else None
        conn.execute(sa.text("DELETE FROM settings WHERE key = 'workspace_dir' AND scope = 'system' AND user_id IS NULL"))
    if row is None and not _used(conn):
        return
    conn.execute(sa.text("DELETE FROM host_state WHERE key = :key"), {"key": KEY})
    conn.execute(
        sa.text("INSERT INTO host_state (key, value, updated_at) VALUES (:key, :value, :now)"),
        {
            "key": KEY, "now": datetime.now(timezone.utc).replace(tzinfo=None),
            "value": json.dumps({"value": stored, "default": OLD_DEFAULT, "old_leaf": OLD_LEAF}, sort_keys=True),
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT value FROM host_state WHERE key = :key"), {"key": KEY}).first()
    if row is None:
        return
    try:
        record = json.loads(row[0])
    except ValueError:
        record = {}
    path = record.get("value") or record.get("default") or OLD_DEFAULT
    has_value = conn.execute(sa.text(
        "SELECT 1 FROM settings WHERE key = 'workspace_dir' AND scope = 'system' AND user_id IS NULL"
    )).first()
    if has_value is None:
        conn.execute(
            sa.text(
                "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, 'workspace_dir', 'system', NULL, :value, :now)"
            ),
            {"id": str(uuid.uuid4()), "value": json.dumps(path), "now": datetime.now(timezone.utc).replace(tzinfo=None)},
        )
    conn.execute(sa.text("DELETE FROM host_state WHERE key = :key"), {"key": KEY})
