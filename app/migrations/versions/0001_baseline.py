"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-06-18

Snapshot of the schema as it stood before alembic was wired up. Mirrors
the SQLModel definitions in app/db/models.py *minus* the recently added
LibraryEntry.storage_provider column - that drift is fixed by 0002 so
existing pre-alembic DBs can be stamped at this revision and then upgraded.

Fresh DBs simply run both migrations in sequence on first boot.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("credential_type", sa.String(), nullable=False),
        sa.Column("data", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_credentials_user_id", "provider_credentials", ["user_id"])
    op.create_index("ix_provider_credentials_provider", "provider_credentials", ["provider"])

    op.create_table(
        "tracks",
        sa.Column("internal_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("album", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("internal_id"),
    )

    op.create_table(
        "track_identifiers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("id_type", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_track_identifiers_track_id", "track_identifiers", ["track_id"])
    op.create_index(
        "ix_track_identifier_type_value",
        "track_identifiers",
        ["id_type", "value"],
    )

    op.create_table(
        "downloads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provider_used", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_downloads_track_id", "downloads", ["track_id"])
    op.create_index("ix_downloads_requested_by", "downloads", ["requested_by"])
    op.create_index("ix_downloads_status", "downloads", ["status"])

    op.create_table(
        "library_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("track_id", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("file_format", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_path"),
    )
    op.create_index("ix_library_entries_track_id", "library_entries", ["track_id"])

    op.create_table(
        "watched_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("auto_download", sa.Boolean(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watched_items_user_id", "watched_items", ["user_id"])

    op.create_table(
        "provider_sources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )

    op.create_table(
        "installed_providers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("package", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("types", sa.String(), nullable=False),
        sa.Column("installed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["provider_sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_installed_providers_source_id", "installed_providers", ["source_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_index("ix_installed_providers_source_id", table_name="installed_providers")
    op.drop_table("installed_providers")
    op.drop_table("provider_sources")
    op.drop_index("ix_watched_items_user_id", table_name="watched_items")
    op.drop_table("watched_items")
    op.drop_index("ix_library_entries_track_id", table_name="library_entries")
    op.drop_table("library_entries")
    op.drop_index("ix_downloads_status", table_name="downloads")
    op.drop_index("ix_downloads_requested_by", table_name="downloads")
    op.drop_index("ix_downloads_track_id", table_name="downloads")
    op.drop_table("downloads")
    op.drop_index("ix_track_identifier_type_value", table_name="track_identifiers")
    op.drop_index("ix_track_identifiers_track_id", table_name="track_identifiers")
    op.drop_table("track_identifiers")
    op.drop_table("tracks")
    op.drop_index("ix_provider_credentials_provider", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_user_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
