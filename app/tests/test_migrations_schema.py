"""
The migrations, on the database under test (SQLite or PostgreSQL): every one
runs from an empty database to the head, the schema they leave is the one the
models declare (``alembic check`` reports nothing, and the structure itself,
partial indexes included, is compared), and the migrations that clean data
before adding a constraint do it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlmodel import SQLModel

from db import models  # noqa: F401 — the tables on the metadata
from db.engine import alembic_config, head_revisions

from tests.database import migration_database, new_engine

NOW = datetime(2026, 10, 4, 12, 0, 0)


def _structure(engine) -> dict:
    """What a database holds per table, in a form that compares: columns
    (name, type family, nullable), indexes and unique constraints (name,
    columns, unique, partial predicate), foreign keys."""
    inspector = sa.inspect(engine)
    structure = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        columns = {column["name"]: (_family(column["type"]), column["nullable"]) for column in inspector.get_columns(table)}
        indexes = {
            (index["name"], tuple(index["column_names"]), bool(index["unique"]),
             _predicate(index.get("dialect_options", {})))
            for index in inspector.get_indexes(table)
        }
        uniques = {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table)
        }
        foreign_keys = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table)
        }
        structure[table] = {"columns": columns, "indexes": indexes, "uniques": uniques, "foreign_keys": foreign_keys}
    return structure


def _family(type_: sa.types.TypeEngine) -> str:
    """The type as the application sees it: the families the two databases
    name differently (VARCHAR/TEXT, INTEGER/BIGINT kept apart)."""
    if isinstance(type_, sa.Boolean):
        return "boolean"
    if isinstance(type_, sa.BigInteger):
        return "bigint"
    if isinstance(type_, sa.Integer):
        return "integer"
    if isinstance(type_, (sa.Float, sa.Numeric)):
        return "float"
    if isinstance(type_, sa.DateTime):
        return "datetime"
    return "text"


def _predicate(options: dict) -> str | None:
    where = options.get("postgresql_where") or options.get("sqlite_where")
    return None if where is None else " ".join(str(where).replace("(", "").replace(")", "").split()).lower()


def test_every_migration_runs_from_an_empty_database_to_the_head():
    engine, _config = migration_database("head")

    with engine.connect() as connection:
        revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert {revision} == head_revisions()


def test_the_migrated_schema_is_the_one_the_models_declare():
    engine, _config = migration_database("head")

    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)

    assert differences == []


def test_the_migrated_structure_equals_the_one_built_from_the_models():
    migrated, _config = migration_database("head")
    built = new_engine()  # create_all of the models

    migrated_structure, built_structure = _structure(migrated), _structure(built)

    assert migrated_structure.keys() == built_structure.keys()
    for table in built_structure:
        assert migrated_structure[table] == built_structure[table], table


def test_the_partial_unique_indexes_exist_with_their_predicates():
    engine, _config = migration_database("head")
    indexes = {
        (table, index["name"]): _predicate(index.get("dialect_options", {}))
        for table in ("library_job_runs", "settings", "provider_credentials")
        for index in sa.inspect(engine).get_indexes(table)
    }

    active = indexes[("library_job_runs", "uq_library_job_active")]  # PostgreSQL spells the list its own way
    assert "queued" in active and "running" in active and "completed" not in active
    assert indexes[("settings", "uq_settings_system_key")] == "user_id is null"
    assert indexes[("provider_credentials", "uq_provider_credential_global")] == "user_id is null"


def test_the_last_migrations_downgrade_and_upgrade_again():
    engine, config = migration_database("head")
    before = _structure(engine)

    command.downgrade(config, "0031_routine_run_progress")
    assert "uq_settings_system_key" not in {i["name"] for i in sa.inspect(engine).get_indexes("settings")}
    command.upgrade(config, "head")

    assert _structure(engine) == before


# ---------------------------------------------------------------------------
# 0032: the constraints, after the rows that break them are gone
# ---------------------------------------------------------------------------


def _insert(connection, table: str, **values) -> None:
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    connection.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({marks})"), values)


def _user_and_track(connection) -> None:
    _insert(connection, "users", id="u", username="u", password_hash="x", role="user", is_active=True, created_at=NOW)
    _insert(connection, "tracks", internal_id="t", title="T", artist="A", album="B", duration_ms=0, created_at=NOW)


def test_0032_keeps_one_copy_of_a_repeated_identifier():
    engine, config = migration_database("0031_routine_run_progress")
    with engine.begin() as connection:
        _user_and_track(connection)
        for identifier in ("a", "b", "c"):
            _insert(connection, "track_identifiers", id=identifier, track_id="t", id_type="isrc", value="ISRC1")
        _insert(connection, "track_identifiers", id="d", track_id="t", id_type="isrc", value="ISRC2")

    command.upgrade(config, "0032_concurrency_constraints")

    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT id, value FROM track_identifiers ORDER BY id")).fetchall()
    assert [tuple(row) for row in rows] == [("a", "ISRC1"), ("d", "ISRC2")]
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        _insert(connection, "track_identifiers", id="e", track_id="t", id_type="isrc", value="ISRC2")


def test_0032_keeps_the_latest_of_two_system_settings_and_of_two_credentials():
    engine, config = migration_database("0031_routine_run_progress")
    older, newer = datetime(2026, 1, 1), datetime(2026, 2, 1)
    with engine.begin() as connection:
        _user_and_track(connection)
        _insert(connection, "settings", id="s-old", key="k", scope="system", user_id=None, value="1", updated_at=older)
        _insert(connection, "settings", id="s-new", key="k", scope="system", user_id=None, value="2", updated_at=newer)
        _insert(connection, "settings", id="s-user", key="k", scope="user", user_id="u", value="3", updated_at=older)
        for row_id, stamp in (("c-old", older), ("c-new", newer)):
            _insert(connection, "provider_credentials", id=row_id, user_id=None, provider="p", credential_type="api_key",
                    data=json.dumps({"api_key": row_id}), updated_at=stamp)
        for row_id, stamp in (("u-old", older), ("u-new", newer)):
            _insert(connection, "provider_credentials", id=row_id, user_id="u", provider="p", credential_type="api_key",
                    data=json.dumps({"api_key": row_id}), updated_at=stamp)

    command.upgrade(config, "0032_concurrency_constraints")

    with engine.connect() as connection:
        settings = {row[0] for row in connection.execute(sa.text("SELECT id FROM settings"))}
        credentials = {row[0] for row in connection.execute(sa.text("SELECT id FROM provider_credentials"))}
    assert settings == {"s-new", "s-user"}
    assert credentials == {"c-new", "u-new"}
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        _insert(connection, "settings", id="s-again", key="k", scope="system", user_id=None, value="9", updated_at=NOW)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        _insert(connection, "provider_credentials", id="c-again", user_id=None, provider="p",
                credential_type="api_key", data="{}", updated_at=NOW)


# ---------------------------------------------------------------------------
# 0033: a file size past 2 GiB
# ---------------------------------------------------------------------------


def test_0033_a_size_of_several_gigabytes_fits():
    engine, config = migration_database("0032_concurrency_constraints")
    command.upgrade(config, "0033_file_sizes_bigint")

    with engine.begin() as connection:
        _insert(connection, "duplicate_groups", id="g", recording_id="r", status="proposed", created_at=NOW, updated_at=NOW)
        _insert(connection, "duplicate_members", id="m", group_id="g", sync_id="s", path="p", title="T", artist="A",
                size=6_000_000_000)
        _insert(connection, "duplicate_fingerprint_cache", id="f", path="p", size=6_000_000_000, computed_at=NOW)

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT size FROM duplicate_members")).scalar() == 6_000_000_000
        assert connection.execute(sa.text("SELECT size FROM duplicate_fingerprint_cache")).scalar() == 6_000_000_000


def test_0033_keeps_the_sizes_and_the_unique_cache_key():
    engine, config = migration_database("0032_concurrency_constraints")
    with engine.begin() as connection:
        _insert(connection, "duplicate_fingerprint_cache", id="f", path="p", size=123, computed_at=NOW)

    command.upgrade(config, "0033_file_sizes_bigint")

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT size FROM duplicate_fingerprint_cache")).scalar() == 123
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        _insert(connection, "duplicate_fingerprint_cache", id="g", path="p", size=123, computed_at=NOW)


# ---------------------------------------------------------------------------
# A database that is not in the state the migrations assume
# ---------------------------------------------------------------------------


def _provider_indexes(engine) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes("user_saved_track_providers")
            if i["column_names"] == ["saved_track_id"]}


def test_0034_renames_the_index_it_finds():
    engine, config = migration_database("0033_file_sizes_bigint")
    assert _provider_indexes(engine) == {"ix_ustp_saved_track_id"}

    command.upgrade(config, "0034_saved_track_provider_index_name")

    assert _provider_indexes(engine) == {"ix_user_saved_track_providers_saved_track_id"}


def test_0034_on_a_database_that_already_has_the_models_index_and_not_the_old_one():
    """The live state: the model's name is there, the old one never was."""
    engine, config = migration_database("0033_file_sizes_bigint")
    with engine.begin() as connection:
        connection.execute(sa.text("DROP INDEX ix_ustp_saved_track_id"))
        connection.execute(sa.text(
            "CREATE INDEX ix_user_saved_track_providers_saved_track_id ON user_saved_track_providers (saved_track_id)"))

    command.upgrade(config, "0034_saved_track_provider_index_name")

    assert _provider_indexes(engine) == {"ix_user_saved_track_providers_saved_track_id"}
    command.downgrade(config, "0033_file_sizes_bigint")
    assert _provider_indexes(engine) == {"ix_ustp_saved_track_id"}
    command.upgrade(config, "head")
    assert _provider_indexes(engine) == {"ix_user_saved_track_providers_saved_track_id"}


def test_0034_with_both_indexes_present_keeps_one():
    engine, config = migration_database("0033_file_sizes_bigint")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE INDEX ix_user_saved_track_providers_saved_track_id ON user_saved_track_providers (saved_track_id)"))

    command.upgrade(config, "0034_saved_track_provider_index_name")

    assert _provider_indexes(engine) == {"ix_user_saved_track_providers_saved_track_id"}


def test_0032_on_a_database_that_already_has_its_constraints():
    engine, config = migration_database("0031_routine_run_progress")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE UNIQUE INDEX uq_track_identifier ON track_identifiers (track_id, id_type, value)"))
        connection.execute(sa.text(
            "CREATE UNIQUE INDEX uq_settings_system_key ON settings (key, scope) WHERE user_id IS NULL"))
        connection.execute(sa.text(
            "CREATE UNIQUE INDEX uq_provider_credential_user ON provider_credentials (user_id, provider, credential_type)"))
        connection.execute(sa.text(
            "CREATE UNIQUE INDEX uq_provider_credential_global ON provider_credentials (provider, credential_type) "
            "WHERE user_id IS NULL"))

    command.upgrade(config, "head")  # nothing fails, nothing is created twice
    command.downgrade(config, "0031_routine_run_progress")  # and each goes, as the index or constraint it is

    names = {i["name"] for t in ("track_identifiers", "settings", "provider_credentials")
             for i in sa.inspect(engine).get_indexes(t)}
    assert not names & {"uq_track_identifier", "uq_settings_system_key", "uq_provider_credential_user",
                        "uq_provider_credential_global"}


def test_0033_goes_down_to_32_bit_and_up_again():
    engine, config = migration_database("head")

    command.downgrade(config, "0032_concurrency_constraints")
    command.upgrade(config, "head")

    sizes = {c["name"]: c["type"] for c in sa.inspect(engine).get_columns("duplicate_members")}
    assert isinstance(sizes["size"], sa.BigInteger)


# ---------------------------------------------------------------------------
# The raw SQL of the old migrations, on this database
# ---------------------------------------------------------------------------


def test_the_data_migrations_work_on_rows_of_the_old_schema():
    """0007, 0010, 0020 and 0021 move real data with SQL: run them over rows."""
    engine, config = migration_database("0006")
    with engine.begin() as connection:
        _insert(connection, "users", id="u", username="u", password_hash="x", role="user", is_active=True, created_at=NOW)
        track = {"title": "Song", "artist": "Band", "album": "Record", "duration_ms": 1000, "provider": "p",
                 "provider_id": "x1", "external_ids": [{"type": "isrc", "value": "ISRC1"}]}
        for row_id, stamp in (("s1", datetime(2026, 1, 1)), ("s2", datetime(2026, 1, 2))):
            _insert(connection, "user_saved_tracks", id=row_id, user_id="u", dedup_key=row_id, dedup_type="isrc",
                    track_data=json.dumps(track), saved_at=stamp)
            _insert(connection, "user_saved_track_providers", id=f"p-{row_id}", saved_track_id=row_id, provider="p",
                    provider_track_id="x1", synced_at=stamp)

    command.upgrade(config, "head")  # 0007 collapses the two likes of one song into one

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM user_saved_tracks")).scalar() == 1
        assert connection.execute(sa.text("SELECT COUNT(*) FROM tracks")).scalar() == 1
        assert connection.execute(sa.text("SELECT COUNT(*) FROM track_identifiers")).scalar() == 1
        assert connection.execute(sa.text("SELECT COUNT(*) FROM user_saved_track_providers")).scalar() == 1
