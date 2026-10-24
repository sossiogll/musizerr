"""normalize saved track providers into separate table

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-20

Moves per-provider info (source_provider, synced_at, stream_url) from
user_saved_tracks into a new user_saved_track_providers table.
Each saved track can now have entries from multiple providers.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # moves data to a new table and drops three columns of user_saved_tracks


def upgrade() -> None:
    op.create_table(
        "user_saved_track_providers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("saved_track_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_track_id", sa.String(), nullable=False, server_default=""),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("stream_url", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["saved_track_id"], ["user_saved_tracks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ustp_saved_track_id",
        "user_saved_track_providers",
        ["saved_track_id"],
    )
    op.create_index(
        "ix_ustp_track_provider",
        "user_saved_track_providers",
        ["saved_track_id", "provider"],
        unique=True,
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, source_provider, saved_at, synced_at "
            "FROM user_saved_tracks WHERE source_provider IS NOT NULL"
        )
    ).fetchall()

    if rows:
        import uuid
        for row in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO user_saved_track_providers "
                    "(id, saved_track_id, provider, provider_track_id, added_at, synced_at) "
                    "VALUES (:id, :saved_track_id, :provider, '', :added_at, :synced_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "saved_track_id": row[0],
                    "provider": row[1],
                    "added_at": row[2],
                    "synced_at": row[3],
                },
            )

    with op.batch_alter_table("user_saved_tracks") as batch_op:
        batch_op.drop_column("source_provider")
        batch_op.drop_column("synced_at")


def downgrade() -> None:
    with op.batch_alter_table("user_saved_tracks") as batch_op:
        batch_op.add_column(sa.Column("source_provider", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("synced_at", sa.DateTime(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE user_saved_tracks SET "
            "source_provider = ("
            "  SELECT provider FROM user_saved_track_providers "
            "  WHERE saved_track_id = user_saved_tracks.id LIMIT 1"
            "), "
            "synced_at = ("
            "  SELECT synced_at FROM user_saved_track_providers "
            "  WHERE saved_track_id = user_saved_tracks.id "
            "  ORDER BY synced_at DESC LIMIT 1"
            ")"
        )
    )

    op.drop_table("user_saved_track_providers")
