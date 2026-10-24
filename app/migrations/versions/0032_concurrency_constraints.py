"""unique constraints for the rows two writers can race to create

Revision ID: 0032_concurrency_constraints
Revises: 0031_routine_run_progress
Create Date: 2026-10-04

The code that creates these rows looked first and inserted if nothing was
there. With one writer at a time that held; with several (PostgreSQL) two
writers can both find nothing and both insert. The database now refuses the
second one, and the writers use ``INSERT ... ON CONFLICT`` (``db/upsert.py``):

- ``track_identifiers``: an identifier is on a track once
  (``uq_track_identifier``);
- ``settings``: one system row per key and scope
  (``uq_settings_system_key``, partial: a UNIQUE treats the NULL ``user_id``
  of two system rows as different);
- ``provider_credentials``: one credential per user, provider and type
  (``uq_provider_credential_user``), and one global one
  (``uq_provider_credential_global``, partial).

Rows that already break a rule are removed first: a repeated identifier keeps
one copy; of two settings or credentials the most recently updated stays (the
writers always read the first row they found, so the others never counted).
The downgrade drops the constraints. A database that already has one of them
(an index or constraint of that name) keeps it: each is created only if missing,
and dropped only if present.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_concurrency_constraints"
down_revision: Union[str, None] = "0031_routine_run_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # deletes duplicate rows and adds unique constraints (tightens)

_NO_USER = sa.text("user_id IS NULL")


def _keep_latest(conn, table: str, partition: str, condition: str) -> None:
    """Of the rows of ``table`` matching ``condition`` that share a
    ``partition``, keep the most recently updated."""
    conn.execute(sa.text(
        f"DELETE FROM {table} WHERE {condition} AND id NOT IN ("
        f"SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
        f"PARTITION BY {partition} ORDER BY updated_at DESC, id DESC) AS n "
        f"FROM {table} WHERE {condition}) AS ranked WHERE n = 1)"
    ))


def _has(table: str, name: str) -> bool:
    """An index or a unique constraint called ``name`` is on ``table``: a
    database that already has it keeps it."""
    inspector = sa.inspect(op.get_bind())
    return name in {index["name"] for index in inspector.get_indexes(table)} | {
        constraint["name"] for constraint in inspector.get_unique_constraints(table)
    }


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text(
        "DELETE FROM track_identifiers WHERE id NOT IN ("
        "SELECT MIN(id) FROM track_identifiers GROUP BY track_id, id_type, value)"
    ))
    if not _has("track_identifiers", "uq_track_identifier"):
        with op.batch_alter_table("track_identifiers") as batch:
            batch.create_unique_constraint("uq_track_identifier", ["track_id", "id_type", "value"])

    _keep_latest(conn, "settings", "key, scope", "user_id IS NULL")
    if not _has("settings", "uq_settings_system_key"):
        op.create_index(
            "uq_settings_system_key", "settings", ["key", "scope"], unique=True,
            sqlite_where=_NO_USER, postgresql_where=_NO_USER,
        )

    _keep_latest(conn, "provider_credentials", "user_id, provider, credential_type", "user_id IS NOT NULL")
    _keep_latest(conn, "provider_credentials", "provider, credential_type", "user_id IS NULL")
    if not _has("provider_credentials", "uq_provider_credential_user"):
        with op.batch_alter_table("provider_credentials") as batch:
            batch.create_unique_constraint(
                "uq_provider_credential_user", ["user_id", "provider", "credential_type"],
            )
    if not _has("provider_credentials", "uq_provider_credential_global"):
        op.create_index(
            "uq_provider_credential_global", "provider_credentials", ["provider", "credential_type"], unique=True,
            sqlite_where=_NO_USER, postgresql_where=_NO_USER,
        )


def _drop(table: str, name: str) -> None:
    """Drops ``name`` from ``table`` if it is there, as the constraint or the
    index it is."""
    inspector = sa.inspect(op.get_bind())
    if name in {constraint["name"] for constraint in inspector.get_unique_constraints(table)}:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="unique")
    elif name in {index["name"] for index in inspector.get_indexes(table)}:
        op.drop_index(name, table_name=table)


def downgrade() -> None:
    _drop("provider_credentials", "uq_provider_credential_global")
    _drop("provider_credentials", "uq_provider_credential_user")
    _drop("settings", "uq_settings_system_key")
    _drop("track_identifiers", "uq_track_identifier")
