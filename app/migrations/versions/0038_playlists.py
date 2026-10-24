"""playlists: the playlists of a user, their tracks and their copies on the sync providers

Revision ID: 0038_playlists
Revises: 0037_provider_login
Create Date: 2026-10-10

Playlists kept by Musizerr (``services/playlists.py``): ``playlists`` (one per
user and source for the followed ones), ``playlist_tracks`` (the tracks in
order; a track is in a playlist once) and ``playlist_syncs`` (the copy of a
playlist on a sync provider, on its owner's own account: its id there and what
was last pushed). ``playlist_syncs.playlist_id`` is not a foreign key on
purpose: the row outlives a deleted playlist until the routine has deleted the
copy on the provider.

New tables only, nothing to backfill. The downgrade drops them.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_playlists"
down_revision: Union[str, None] = "0037_provider_login"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "expand"  # adds tables


def upgrade() -> None:
    op.create_table(
        "playlists",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False, server_default="own"),
        sa.Column("source_provider", sa.String(), nullable=True),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("auto_download", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tracks_unavailable_code", sa.String(), nullable=True),
        sa.Column("tracks_unavailable_message", sa.String(), nullable=True),
        sa.Column("refresh_error_code", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source_provider", "source_id", name="uq_playlist_source"),
    )
    op.create_index("ix_playlists_user_id", "playlists", ["user_id"])

    op.create_table(
        "playlist_tracks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("download_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["playlists.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),
    )
    op.create_index("ix_playlist_tracks_track_id", "playlist_tracks", ["track_id"])
    op.create_index("ix_playlist_tracks_playlist_position", "playlist_tracks", ["playlist_id", "position"])

    op.create_table(
        "playlist_syncs",
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("sync_provider", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("playlist_id", "sync_provider"),
    )
    op.create_index("ix_playlist_syncs_user_id", "playlist_syncs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_playlist_syncs_user_id", table_name="playlist_syncs")
    op.drop_table("playlist_syncs")
    op.drop_index("ix_playlist_tracks_playlist_position", table_name="playlist_tracks")
    op.drop_index("ix_playlist_tracks_track_id", table_name="playlist_tracks")
    op.drop_table("playlist_tracks")
    op.drop_index("ix_playlists_user_id", table_name="playlists")
    op.drop_table("playlists")
