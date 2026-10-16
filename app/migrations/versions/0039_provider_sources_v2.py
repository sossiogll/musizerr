"""provider sources v2: catalogs pinned by digest, a credential per source, approved installs

Revision ID: 0039_provider_sources_v2
Revises: 0038_playlists
Create Date: 2026-10-13

ADR-0006, phase 4. A source is the URL of a catalog (version 2); the providers it lists
are OCI images pinned by digest that the gateway pulls.

``provider_sources`` keeps its rows and gains ``credential`` (the token of a private
catalog and its images; NULL when none; stored like the provider credentials, which are
not encrypted at rest) and ``allow_insecure_registry`` (plain HTTP to the image registry
of THIS source only; off).

``installed_providers`` changes shape completely: a pip package (``package``,
``version``, ``types``) becomes an image pinned by digest with what the admin approved
for it (roles, permissions as JSON, the API version) and who approved it and when. The
rows of the pip era describe packages that no longer exist as a way to install anything
(and the gateway never ran them), so they are DROPPED: the table is recreated empty.
The admin installs again from a catalog.

The downgrade recreates the old shape, empty.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_provider_sources_v2"
down_revision: Union[str, None] = "0038_playlists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_installed_providers_source_id", table_name="installed_providers")
    op.drop_table("installed_providers")

    with op.batch_alter_table("provider_sources") as batch:
        batch.add_column(sa.Column("credential", sa.String(), nullable=True))
        batch.add_column(sa.Column("allow_insecure_registry", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "installed_providers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("image", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("provider_api_version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("roles", sa.String(), nullable=False),
        sa.Column("permissions", sa.String(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("installed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["provider_sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_installed_providers_source_id", "installed_providers", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_installed_providers_source_id", table_name="installed_providers")
    op.drop_table("installed_providers")

    with op.batch_alter_table("provider_sources") as batch:
        batch.drop_column("allow_insecure_registry")
        batch.drop_column("credential")

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
