"""canonicalize user_saved_tracks onto the tracks table

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-20

user_saved_tracks previously owned a private track_data JSON blob per saved
track, independent of the tracks table used by download history. That meant
the same physical track could have two unrelated metadata snapshots, and
saving it from a second provider silently overwrote the first instead of
merging (see db/track_store.py).

This migration points user_saved_tracks at the canonical tracks table via a
track_id FK, replacing dedup_key/dedup_type/track_data. The backfill resolves
each existing saved track onto an existing or new tracks row by reusing
db.track_store.find_or_create_track() — the exact same identity+merge logic
(exact external-id match, fuzzy title+artist fallback, core.merge.enrich())
that downloads and ongoing library syncs use going forward. Re-implementing
a parallel matching routine in raw SQL here would both duplicate that logic
and silently skip the metadata merge for matches found during backfill.

Saved tracks for the same user that still resolve to the same canonical
track after that (e.g. a historical edge case the old per-row dedup_key
text comparison missed) are collapsed into one row, keeping the earliest
saved_at and re-pointing user_saved_track_providers.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlmodel import Session


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # re-homes saved tracks onto tracks, drops columns and tightens a foreign key


def upgrade() -> None:
    import json

    from core.serialization import deserialize_track
    from db.track_store import find_or_create_track

    op.add_column("user_saved_tracks", sa.Column("track_id", sa.String(), nullable=True))

    conn = op.get_bind()
    session = Session(bind=conn)

    # ------------------------------------------------------------------
    # 1. Resolve (or create+merge) the canonical track for every saved
    #    track, oldest first so merges are deterministic.
    # ------------------------------------------------------------------
    rows = conn.execute(
        sa.text(
            "SELECT id, track_data, saved_at FROM user_saved_tracks ORDER BY saved_at ASC"
        )
    ).fetchall()

    for row_id, track_data_json, _saved_at in rows:
        track = deserialize_track(json.loads(track_data_json or "{}"))
        record = find_or_create_track(track, session)
        conn.execute(
            sa.text("UPDATE user_saved_tracks SET track_id = :tid WHERE id = :id"),
            {"tid": record.internal_id, "id": row_id},
        )

    # ------------------------------------------------------------------
    # 2. Collapse saved tracks that, for the same user, now resolve to the
    #    same canonical track (defensive — find_or_create_track already
    #    matches/merges within step 1, this only catches leftover edge
    #    cases such as historical rows that predate identifier tagging).
    # ------------------------------------------------------------------
    dupes = conn.execute(
        sa.text(
            "SELECT user_id, track_id FROM user_saved_tracks "
            "GROUP BY user_id, track_id HAVING COUNT(*) > 1"
        )
    ).fetchall()

    for dupe_user_id, dupe_track_id in dupes:
        saved_ats = conn.execute(
            sa.text("SELECT id, saved_at FROM user_saved_tracks WHERE user_id = :user_id AND track_id = :track_id"),
            {"user_id": dupe_user_id, "track_id": dupe_track_id},
        ).fetchall()
        ids = [r[0] for r in saved_ats]
        keep_id = min(saved_ats, key=lambda r: r[1])[0]

        for rid in ids:
            if rid == keep_id:
                continue
            providers_to_move = conn.execute(
                sa.text("SELECT id, provider FROM user_saved_track_providers WHERE saved_track_id = :id"),
                {"id": rid},
            ).fetchall()
            for prov_row_id, provider in providers_to_move:
                clash = conn.execute(
                    sa.text(
                        "SELECT 1 FROM user_saved_track_providers "
                        "WHERE saved_track_id = :keep_id AND provider = :provider"
                    ),
                    {"keep_id": keep_id, "provider": provider},
                ).first()
                if clash:
                    conn.execute(
                        sa.text("DELETE FROM user_saved_track_providers WHERE id = :id"),
                        {"id": prov_row_id},
                    )
                else:
                    conn.execute(
                        sa.text(
                            "UPDATE user_saved_track_providers SET saved_track_id = :keep_id WHERE id = :id"
                        ),
                        {"keep_id": keep_id, "id": prov_row_id},
                    )
            conn.execute(sa.text("DELETE FROM user_saved_tracks WHERE id = :id"), {"id": rid})

    # ------------------------------------------------------------------
    # 3. Finalize schema.
    # ------------------------------------------------------------------
    op.drop_index("ix_ust_user_dedup", table_name="user_saved_tracks")

    with op.batch_alter_table("user_saved_tracks") as batch_op:
        batch_op.alter_column("track_id", existing_type=sa.String(), nullable=False)
        batch_op.drop_column("dedup_key")
        batch_op.drop_column("dedup_type")
        batch_op.drop_column("track_data")
        batch_op.create_foreign_key("fk_ust_track_id", "tracks", ["track_id"], ["internal_id"])

    op.create_index("ix_user_saved_tracks_track_id", "user_saved_tracks", ["track_id"])
    op.create_index(
        "ix_ust_user_track", "user_saved_tracks", ["user_id", "track_id"], unique=True,
    )


def downgrade() -> None:
    """Best-effort: restores dedup_key/dedup_type/track_data from the linked
    canonical track. Saved-track rows collapsed during upgrade() (see step 2
    above) are NOT split back apart, and tracks rows created by the backfill
    are left in place — matching 0006's downgrade, which is also not a
    perfect mirror of its own upgrade."""
    with op.batch_alter_table("user_saved_tracks") as batch_op:
        batch_op.drop_constraint("fk_ust_track_id", type_="foreignkey")
        batch_op.add_column(sa.Column("dedup_key", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("dedup_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("track_data", sa.String(), nullable=True))

    op.drop_index("ix_ust_user_track", table_name="user_saved_tracks")
    op.drop_index("ix_user_saved_tracks_track_id", table_name="user_saved_tracks")

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, track_id FROM user_saved_tracks")).fetchall()

    for row_id, track_id in rows:
        track = conn.execute(
            sa.text("SELECT title, artist, track_data FROM tracks WHERE internal_id = :id"),
            {"id": track_id},
        ).first()
        if not track:
            continue
        title, artist, track_data = track

        ident = conn.execute(
            sa.text(
                "SELECT id_type, value FROM track_identifiers WHERE track_id = :id "
                "AND id_type IN ('isrc', 'mbid') ORDER BY id_type LIMIT 1"
            ),
            {"id": track_id},
        ).first()
        if ident:
            dedup_type, dedup_key = ident[0], ident[1]
        else:
            dedup_type = "fuzzy"
            dedup_key = f"{(title or '').strip().lower()}::{(artist or '').strip().lower()}"

        conn.execute(
            sa.text(
                "UPDATE user_saved_tracks SET dedup_key = :dedup_key, dedup_type = :dedup_type, "
                "track_data = :track_data WHERE id = :id"
            ),
            {"dedup_key": dedup_key, "dedup_type": dedup_type, "track_data": track_data, "id": row_id},
        )

    with op.batch_alter_table("user_saved_tracks") as batch_op:
        batch_op.alter_column("dedup_key", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("dedup_type", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("track_data", existing_type=sa.String(), nullable=False)
        batch_op.drop_column("track_id")

    op.create_index(
        "ix_ust_user_dedup", "user_saved_tracks", ["user_id", "dedup_key"], unique=True,
    )
