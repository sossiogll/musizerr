"""download requests: the approval policy's record, and per-user policy overrides

Revision ID: 0035_download_requests
Revises: 0034_saved_track_provider_index_name
Create Date: 2026-10-04

- ``download_requests``: one row per request of a user the approval policy
  governs (``core/download_policy.py``), the ones approved at once too: the
  requester, the kind (track, album, playlist, artist, liked_elsewhere), the
  target and display fields, the ``payload`` approval runs later, the tracks
  that needed a download, the status (``pending`` / ``approved`` / ``denied``
  / ``cancelled``), whether the policy approved it (``auto``) and why
  (``reason``), who decided and when. ``uq_download_request_pending`` allows
  one pending request per user and target; the indexes serve the quota (a
  user's approved requests in a window) and the admin's pending list.
- ``user_request_policies``: what an admin changed for one user (a mode or
  threshold per kind, the quota); the general policy is in ``settings``.

Nothing is backfilled: downloads made before this migration were never
requests, and the quota counts only the requests recorded from now on. The
downgrade drops both tables.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_download_requests"
down_revision: Union[str, None] = "0034_saved_track_provider_index_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PENDING = sa.text("status = 'pending'")


def upgrade() -> None:
    op.create_table(
        "download_requests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target_provider", sa.String(), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("subtitle", sa.String(), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("payload", sa.String(), nullable=False, server_default="{}"),
        sa.Column("needed_tracks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_tracks", sa.Integer(), nullable=True),
        sa.Column("dispatched_to", sa.String(), nullable=True),
        sa.Column("add_to_favorites", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("only_missing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_download_requests_user_id", "download_requests", ["user_id"])
    op.create_index(
        "ix_download_requests_user_status_decided", "download_requests", ["user_id", "status", "decided_at"],
    )
    op.create_index("ix_download_requests_status_created", "download_requests", ["status", "created_at"])
    op.create_index(
        "uq_download_request_pending", "download_requests", ["user_id", "kind", "target_provider", "target_id"],
        unique=True, sqlite_where=_PENDING, postgresql_where=_PENDING,
    )

    op.create_table(
        "user_request_policies",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("rules", sa.String(), nullable=False, server_default="{}"),
        sa.Column("quota_tracks", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_request_policies")
    op.drop_index("uq_download_request_pending", table_name="download_requests")
    op.drop_index("ix_download_requests_status_created", table_name="download_requests")
    op.drop_index("ix_download_requests_user_status_decided", table_name="download_requests")
    op.drop_index("ix_download_requests_user_id", table_name="download_requests")
    op.drop_table("download_requests")
