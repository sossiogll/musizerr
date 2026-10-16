"""favorites: user_favorites, like matching, one intent per track; auto-like removed

Revision ID: 0020_favorites
Revises: 0019_host_url_refs
Create Date: 2026-09-28

- ``user_favorites``: the user's favorites, always tracks in the library.
- ``user_saved_tracks.library_track_id`` / ``matched_at``: the library track a
  like imported from another provider was matched to (set once).
- ``pending_likes``: one intent per (user, track); ``sync_provider`` becomes
  nullable (NULL = "make it a favorite when the download lands"). Duplicate
  intents of the same user and track keep the oldest.
- The ``auto_like_on_sync`` setting no longer exists: every stored row (per
  user or system) is deleted. A download requested by a user now always adds
  the track to their favorites when it lands.
- ``autolike_reconcile_*`` settings become ``like_reconcile_*``.

Idempotent: deleting and renaming rows that are already gone changes nothing.
The downgrade restores the schema and the old setting names; it does not
recreate ``auto_like_on_sync`` rows (without one the old default, False,
applied).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_favorites"
down_revision: Union[str, None] = "0019_host_url_refs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REMOVED_SETTING = "auto_like_on_sync"
RENAMED_SETTINGS = {
    "autolike_reconcile_interval_seconds": "like_reconcile_interval_seconds",
    "autolike_reconcile_max_age_hours": "like_reconcile_max_age_hours",
}


def _rename_settings(conn, renames: dict[str, str]) -> None:
    for old, new in renames.items():
        # A row under the new name wins: the old one is only dropped.
        conn.execute(
            sa.text(
                "DELETE FROM settings WHERE key = :old AND EXISTS ("
                "SELECT 1 FROM settings AS s WHERE s.key = :new AND s.scope = settings.scope "
                "AND COALESCE(s.user_id, '') = COALESCE(settings.user_id, ''))"
            ),
            {"old": old, "new": new},
        )
        conn.execute(sa.text("UPDATE settings SET key = :new WHERE key = :old"), {"old": old, "new": new})


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        "user_favorites",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="heart"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "track_id", name="uq_user_favorite"),
    )
    op.create_index("ix_user_favorites_user_id", "user_favorites", ["user_id"])
    op.create_index("ix_user_favorites_track_id", "user_favorites", ["track_id"])

    with op.batch_alter_table("user_saved_tracks") as batch:
        batch.add_column(sa.Column("library_track_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("matched_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_user_saved_tracks_library_track_id", ["library_track_id"])
        batch.create_foreign_key("fk_ust_library_track", "tracks", ["library_track_id"], ["internal_id"])

    # One intent per (user, track): keep the oldest.
    conn.execute(sa.text(
        "DELETE FROM pending_likes WHERE id NOT IN ("
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
        "PARTITION BY user_id, track_id ORDER BY created_at, id) AS n FROM pending_likes) AS ranked WHERE n = 1)"
    ))
    with op.batch_alter_table("pending_likes") as batch:
        batch.drop_constraint("uq_pending_like", type_="unique")
        batch.alter_column("sync_provider", existing_type=sa.String(), nullable=True)
        batch.create_unique_constraint("uq_pending_like", ["user_id", "track_id"])

    conn.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": REMOVED_SETTING})
    _rename_settings(conn, RENAMED_SETTINGS)


def downgrade() -> None:
    conn = op.get_bind()
    _rename_settings(conn, {new: old for old, new in RENAMED_SETTINGS.items()})

    # The old schema has no "favorite on arrival" phase.
    conn.execute(sa.text("DELETE FROM pending_likes WHERE sync_provider IS NULL"))
    with op.batch_alter_table("pending_likes") as batch:
        batch.drop_constraint("uq_pending_like", type_="unique")
        batch.alter_column("sync_provider", existing_type=sa.String(), nullable=False)
        batch.create_unique_constraint("uq_pending_like", ["user_id", "track_id", "sync_provider"])

    with op.batch_alter_table("user_saved_tracks") as batch:
        batch.drop_constraint("fk_ust_library_track", type_="foreignkey")
        batch.drop_index("ix_user_saved_tracks_library_track_id")
        batch.drop_column("matched_at")
        batch.drop_column("library_track_id")

    op.drop_index("ix_user_favorites_track_id", table_name="user_favorites")
    op.drop_index("ix_user_favorites_user_id", table_name="user_favorites")
    op.drop_table("user_favorites")
