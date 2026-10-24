"""removed_favorites: a favorite the user took off stays off

Revision ID: 0026_removed_favorites
Revises: 0025_track_data_without_identifiers
Create Date: 2026-10-03

Today a removed favorite stayed removed only because the imported like kept
its ``matched_at``: a re-import that resolved the like to another record, or
a wipe of the imported likes followed by a new sync, brought it back. The
removal is now remembered by what names the song (``core.matching.
song_keys``: its stable identifiers and its title identity with each artist),
one row per key, the keys of one removal sharing ``group_id``.

Backfill: a like that was matched to a library track whose user has no
favorite on it is a favorite the user took off (the like matching always
makes the favorite when it matches, and the only things that take one off are
the user's own heart and the identity cleanup, which also unmatches the
like). Those are recorded, so the removals made before this release are kept
too. Idempotent: a key already recorded is not recorded again.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_removed_favorites"
down_revision: Union[str, None] = "0025_track_data_without_identifiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds a table


def upgrade() -> None:
    op.create_table(
        "removed_favorites",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_removed_favorite_key"),
    )
    op.create_index("ix_removed_favorites_user_id", "removed_favorites", ["user_id"])
    op.create_index("ix_removed_favorites_key", "removed_favorites", ["key"])
    op.create_index("ix_removed_favorites_group_id", "removed_favorites", ["group_id"])
    _backfill(op.get_bind())


def _backfill(conn) -> None:
    from musizerr_sdk.identifiers import Identifier
    from core.matching import song_keys
    from core.serialization import deserialize_track

    removed = conn.execute(sa.text(
        "SELECT DISTINCT s.user_id, s.track_id, s.library_track_id FROM user_saved_tracks s "
        "WHERE s.library_track_id IS NOT NULL AND s.matched_at IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM user_favorites f WHERE f.user_id = s.user_id AND f.track_id = s.library_track_id)"
    )).fetchall()
    if not removed:
        return

    cache: dict[str, frozenset[str]] = {}

    def keys_of(track_id: str) -> frozenset[str]:
        if track_id not in cache:
            row = conn.execute(
                sa.text("SELECT title, artist, album, duration_ms, track_data FROM tracks WHERE internal_id = :id"),
                {"id": track_id},
            ).first()
            if row is None:
                cache[track_id] = frozenset()
            else:
                try:
                    data = json.loads(row[4]) if row[4] else {}
                except ValueError:
                    data = {}
                data = dict(data) if isinstance(data, dict) else {}
                data.pop("external_ids", None)
                data.setdefault("title", row[0])
                data.setdefault("artist", row[1])
                data.setdefault("album", row[2])
                data.setdefault("duration_ms", row[3] or 0)
                track = deserialize_track(data)
                for id_type, value in conn.execute(
                    sa.text("SELECT id_type, value FROM track_identifiers WHERE track_id = :id"), {"id": track_id}
                ):
                    track.add_id(Identifier.from_dict({"type": id_type, "value": value}))
                cache[track_id] = song_keys(track)
        return cache[track_id]

    recorded = {(user_id, key) for user_id, key in conn.execute(sa.text("SELECT user_id, key FROM removed_favorites"))}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    groups: dict[tuple[str, str], str] = {}
    for user_id, liked_id, library_id in removed:
        group = groups.setdefault((user_id, library_id), str(uuid.uuid4()))
        for key in sorted(keys_of(liked_id) | keys_of(library_id)):
            if (user_id, key) in recorded:
                continue
            recorded.add((user_id, key))
            conn.execute(
                sa.text("INSERT INTO removed_favorites (id, user_id, key, group_id, removed_at) "
                        "VALUES (:id, :user_id, :key, :group_id, :now)"),
                {"id": str(uuid.uuid4()), "user_id": user_id, "key": key, "group_id": group, "now": now},
            )


def downgrade() -> None:
    op.drop_index("ix_removed_favorites_group_id", table_name="removed_favorites")
    op.drop_index("ix_removed_favorites_key", table_name="removed_favorites")
    op.drop_index("ix_removed_favorites_user_id", table_name="removed_favorites")
    op.drop_table("removed_favorites")
