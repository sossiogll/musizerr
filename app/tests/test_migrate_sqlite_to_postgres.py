"""
``scripts/migrate_sqlite_to_postgres.py``: an existing SQLite database moves
to PostgreSQL completely.

The source is built by the real migrations and filled with rows of EVERY
table, generated from the models (nullable columns both NULL and set,
booleans, datetimes with microseconds, a size past 2 GiB, text with accents,
JSON where the application stores it). Equality is checked here, from the raw
rows of both databases, independently of the tool's own checksum.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from core.utc import as_utc
from db import credential_cipher, models
from db.engine import POSTGRESQL, make_engine, sqlite_url, upgrade_to_head
from scripts import migrate_sqlite_to_postgres as tool

from tests.credentials_support import THIRD_KEY, use_keys
from tests.database import new_engine

pytestmark = pytest.mark.postgresql_only("the target of the migration is PostgreSQL")

ROWS = 3
# What the migrations themselves write in an empty database, whatever the operator holds: the storage
# the old catalog default stood for (migration 0040), in ``settings``.
SEEDED = {"settings": 1}
JSON_COLUMNS = {
    ("settings", "value"), ("provider_credentials", "data"), ("tracks", "track_data"), ("library_job_runs", "params"),
    ("library_job_runs", "providers"), ("library_job_runs", "result"), ("duplicate_groups", "deleted_paths"),
    ("installed_providers", "types"), ("routine_runs", "progress"),
}
# The columns that hold a credential: sealed with CREDENTIALS_KEY (docs/credentials.md).
SEALED_COLUMNS = {("provider_credentials", "data"), ("provider_sources", "credential")}
VALUES = {
    ("settings", "scope"): ["system", "user", "user"],
    ("library_job_runs", "status"): ["queued", "running", "completed"],
    ("library_job_runs", "kind"): ["library_sync"] * 3,
}


def _python_type(column: sa.Column) -> type:
    """The Python type of a column, through the application's own types."""
    return getattr(column.type, "impl", column.type).python_type


def _value(table: sa.Table, column: sa.Column, i: int, keys: dict[str, list]):
    """The value of ``column`` in the ``i``-th row of ``table``."""
    if column.foreign_keys:
        if column.nullable and i == 0:
            return None
        parents = keys[next(iter(column.foreign_keys)).column.table.name]
        return parents[i % len(parents)]
    if column.primary_key:
        return f"{table.name}-{i}"
    if column.nullable and i == 0:
        return None
    if (table.name, column.name) in SEALED_COLUMNS:
        # What the application stores there since migration 0041: sealed, never in clear.
        return credential_cipher.seal(json.dumps({"row": i, "text": "Café ☕ 日本語"}))
    if (table.name, column.name) in VALUES:
        return VALUES[(table.name, column.name)][i]
    if (table.name, column.name) in JSON_COLUMNS:
        return json.dumps({"row": i, "text": "Café ☕ 日本語"})
    kind = _python_type(column)
    if kind is bool:
        return i % 2 == 0
    if kind is int:
        return 5_000_000_000 + i if isinstance(column.type, sa.BigInteger) else i + 1
    if kind is float:
        return i + 0.25
    if kind is datetime:
        return datetime(2026, 3, 1 + i, 12, 30, 15, 123456 + i, tzinfo=timezone.utc)
    return f"{table.name}.{column.name}-{i} Café ☕"


def fill(connection: sa.engine.Connection) -> dict[str, int]:
    """``ROWS`` rows in every table, parents first: the counts per table."""
    keys: dict[str, list] = {}
    for table in tool.APP_TABLES:
        rows = [{c.name: _value(table, c, i, keys) for c in table.columns} for i in range(ROWS)]
        connection.execute(table.insert(), rows)
        primary = list(table.primary_key.columns)[0]
        keys[table.name] = [row[primary.name] for row in rows]
    return {table.name: ROWS for table in tool.APP_TABLES}


def sqlite_source(tmp_path: Path, *, populate: bool = True) -> Path:
    """A SQLite file at the head, as an existing install would have it."""
    path = tmp_path / "musizerr.db"
    engine = make_engine(sqlite_url(path))
    upgrade_to_head(engine)
    if populate:
        with engine.begin() as connection:
            fill(connection)
    engine.dispose()
    return path


@pytest.fixture
def source(tmp_path) -> Path:
    return sqlite_source(tmp_path)


@pytest.fixture
def target():
    """An empty PostgreSQL database, no tables."""
    return new_engine(schema=False)


@pytest.fixture
def config_json(tmp_path, monkeypatch) -> dict:
    """A ``config.json`` in ``DATA_DIR``, as every real install has: migration 0010 imports it into
    ``settings`` on whatever database it runs on, the source's and the target's. Request it BEFORE
    ``source``, so the source is migrated with it too."""
    config = {"search_limit": 7, "metadata_provider_priority": ["first", "second"], "auth_enabled": False, "name": "Café"}
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("DATA_DIR", str(data))
    return config


def _settings(connection) -> list[tuple]:
    rows = connection.execute(sa.text("SELECT id, key, scope, user_id, value, updated_at FROM settings"))
    return sorted((row[0], row[1], row[2], row[3], row[4], str(row[5])) for row in rows)


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _raw_rows(connection, table: sa.Table) -> list[dict]:
    return [dict(row._mapping) for row in connection.execute(sa.text(f'SELECT * FROM "{table.name}"'))]


def _normalize(table: sa.Table, row: dict) -> tuple:
    """A raw row of either database, in one form: booleans as bool, datetimes
    as UTC text with microseconds, with no offset."""
    out = []
    for column in table.columns:
        value = row[column.name]
        if value is not None and _python_type(column) is bool:
            value = bool(value)
        elif value is not None and _python_type(column) is datetime:
            # UTC either way: a migration writes its own timestamps with an offset, the application without
            value = as_utc(datetime.fromisoformat(value) if isinstance(value, str) else value).replace(tzinfo=None).isoformat()
        out.append((column.name, value))
    return tuple(out)


def assert_same_data(source_path: Path, target_engine, *, extra_users: int = 0) -> None:
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    with target_engine.connect() as target:
        for table in tool.APP_TABLES:
            sent = sorted(
                _normalize(table, dict(row)) for row in source.execute(f'SELECT * FROM "{table.name}"')
            )
            held = sorted(_normalize(table, row) for row in _raw_rows(target, table))
            if table.name == models.User.__tablename__:
                held = [row for row in held if dict(row)["id"] != models.SYSTEM_USER_ID or row in sent]
            assert held == sent, table.name
    source.close()


# ---------------------------------------------------------------------------
# A complete copy
# ---------------------------------------------------------------------------


def test_a_dry_run_reports_and_touches_nothing(source, target):
    report = tool.migrate(source, target.url, apply=False)

    assert report.ok and report.exit_code == 0 and not report.committed
    assert {t.table: t.source_rows for t in report.tables} == {
        table.name: ROWS + SEEDED.get(table.name, 0) for table in tool.APP_TABLES
    }
    assert all(t.copied == t.source_rows + t.created for t in report.tables)
    assert _tables(target) == set()  # not even the schema


def test_every_row_of_every_table_arrives_equal(source, target):
    report = tool.migrate(source, target.url, apply=True)

    assert report.ok and report.committed and report.exit_code == 0, report.problems
    assert all(t.checksum_match for t in report.tables)
    assert {t.table for t in report.tables} == {table.name for table in SQLModel.metadata.sorted_tables}
    assert_same_data(source, target)
    with target.connect() as connection:
        for table in tool.APP_TABLES:
            expected = ROWS + (1 if table.name == "users" else 0) + SEEDED.get(table.name, 0)  # the system user
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == expected, table.name
    assert report.credentials == {"checked": ROWS, "unreadable": 0}


def test_the_target_is_at_the_head_and_has_the_types_of_the_models(source, target):
    tool.migrate(source, target.url, apply=True)

    inspector = sa.inspect(target)
    users = {c["name"]: c["type"] for c in inspector.get_columns("users")}
    assert isinstance(users["is_active"], sa.Boolean)
    assert isinstance(users["created_at"], sa.DateTime) and not users["created_at"].timezone
    members = {c["name"]: c["type"] for c in inspector.get_columns("duplicate_members")}
    assert isinstance(members["size"], sa.BigInteger)
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() in tool.head_revisions()
        sent = sa.text("SELECT size FROM duplicate_members ORDER BY size")
        assert [row[0] for row in connection.execute(sent)] == [5_000_000_000 + i for i in range(ROWS)]


def test_the_booleans_and_datetimes_are_values_not_text(source, target):
    tool.migrate(source, target.url, apply=True)

    with target.connect() as connection:
        active = connection.execute(sa.text("SELECT is_active FROM users WHERE id = 'users-0'")).scalar()
        stamp = connection.execute(sa.text("SELECT created_at FROM users WHERE id = 'users-0'")).scalar()
        kind = connection.execute(sa.text("SELECT pg_typeof(is_active)::text FROM users LIMIT 1")).scalar()
    assert active is True and kind == "boolean"
    assert stamp == datetime(2026, 3, 1, 12, 30, 15, 123456)


def test_nulls_stay_nulls_and_values_stay_values(source, target):
    tool.migrate(source, target.url, apply=True)

    with target.connect() as connection:
        rows = connection.execute(sa.text("SELECT user_id FROM provider_credentials ORDER BY id")).fetchall()
    assert [row[0] for row in rows] == [None, "users-1", "users-2"]


def test_the_credentials_arrive_byte_for_byte(source, target):
    tool.migrate(source, target.url, apply=True)

    sent = sqlite3.connect(source).execute("SELECT id, data FROM provider_credentials ORDER BY id").fetchall()
    with target.connect() as connection:
        held = connection.execute(sa.text("SELECT id, data FROM provider_credentials ORDER BY id")).fetchall()
    assert [tuple(row) for row in held] == sent


def test_the_source_tokens_arrive_byte_for_byte_too(source, target):
    tool.migrate(source, target.url, apply=True)

    sent = sqlite3.connect(source).execute("SELECT id, credential FROM provider_sources ORDER BY id").fetchall()
    with target.connect() as connection:
        held = connection.execute(sa.text("SELECT id, credential FROM provider_sources ORDER BY id")).fetchall()
    assert [tuple(row) for row in held] == sent
    assert sent[0][1] is None and all(row[1].startswith("enc:v1:") for row in sent[1:])


def test_without_the_key_the_copy_is_refused_before_anything_is_read(source, target, monkeypatch):
    use_keys(monkeypatch, None)

    for apply in (False, True):
        report = tool.migrate(source, target.url, apply=apply)

        assert report.exit_code == 2 and "CREDENTIALS_KEY" in report.problems[0]
        assert not report.committed and _tables(target) == set()


def test_credentials_the_configured_key_cannot_open_stop_the_copy_and_leave_the_target_empty(source, target, monkeypatch):
    """A copy that moved accounts nobody can read would look like a success."""
    use_keys(monkeypatch, THIRD_KEY)

    report = tool.migrate(source, target.url, apply=True)

    assert not report.ok and report.exit_code == 1 and not report.committed
    assert "cannot be opened with the configured CREDENTIALS_KEY" in report.problems[0]
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM provider_credentials")).scalar() == 0


def test_the_partial_unique_index_works_on_the_copied_data(source, target):
    tool.migrate(source, target.url, apply=True)

    # users-0 has a queued library_sync run: a second active one is refused.
    with pytest.raises(IntegrityError), target.begin() as connection:
        connection.execute(models.LibraryJobRun.__table__.insert(), {
            "id": "second", "user_id": "users-0", "kind": "library_sync", "status": "running",
            "processed": 0, "created_at": datetime(2026, 4, 1),
        })
    with target.begin() as connection:  # a finished one is fine
        connection.execute(models.LibraryJobRun.__table__.insert(), {
            "id": "third", "user_id": "users-0", "kind": "library_sync", "status": "completed",
            "processed": 0, "created_at": datetime(2026, 4, 1),
        })


def test_the_application_works_on_the_copied_database(source, target):
    """Not only equal: the code that reads and writes it does."""
    from sqlmodel import Session, select

    from db import settings_store, track_store, user_service

    tool.migrate(source, target.url, apply=True)

    with Session(target) as session:
        assert len(user_service.list_users(session)) == ROWS  # not the system user
        repo = settings_store.SettingsRepository(session)
        repo.set_system("a_key", 1)
        repo.set_system("a_key", 2)
        assert repo.get_system("a_key") == 2
        assert len(session.exec(select(models.Setting).where(models.Setting.key == "a_key")).all()) == 1
        record = session.exec(select(models.TrackRecord)).first()
        assert track_store.load_track(session, record).title


# ---------------------------------------------------------------------------
# What stops it, and what it replaces
# ---------------------------------------------------------------------------


def test_a_source_behind_the_head_is_refused_with_what_to_do(tmp_path, target):
    path = tmp_path / "old.db"
    engine = make_engine(sqlite_url(path))
    from alembic import command

    from db.engine import alembic_config

    command.upgrade(alembic_config(engine.url), "0031_routine_run_progress")
    engine.dispose()

    report = tool.migrate(path, target.url, apply=True)

    assert not report.ok and report.exit_code == 2
    assert "upgrade the source first" in report.problems[0]
    assert _tables(target) == set()


def test_a_missing_source_is_refused(tmp_path, target):
    report = tool.migrate(tmp_path / "nope.db", target.url, apply=False)

    assert report.exit_code == 2 and "not a file" in report.problems[0]


def test_a_target_that_is_not_postgresql_is_refused(source, tmp_path):
    report = tool.migrate(source, sqlite_url(tmp_path / "other.db"), apply=False)

    assert report.exit_code == 2 and "PostgreSQL" in report.problems[0]


def test_a_target_that_holds_data_is_refused_and_replace_empties_it(source, target):
    assert tool.migrate(source, target.url, apply=True).ok
    with target.begin() as connection:
        connection.execute(models.Setting.__table__.insert(), {
            "id": "stray", "key": "stray", "scope": "system", "user_id": None, "value": "1",
            "updated_at": datetime(2026, 1, 1),
        })

    refused = tool.migrate(source, target.url, apply=True)
    assert refused.exit_code == 2 and "already holds data" in refused.problems[0]
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM settings WHERE id = 'stray'")).scalar() == 1

    replaced = tool.migrate(source, target.url, apply=True, replace=True)
    assert replaced.ok and replaced.committed
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM settings WHERE id = 'stray'")).scalar() == 0
    assert_same_data(source, target)


def test_the_rows_the_migrations_seed_are_replaced_by_the_source(config_json, source, target):
    """The target was empty: what the migrations put in it (``config.json`` into ``settings``) is
    not the operator's data. The source is authoritative: no ``--replace`` needed."""
    with sqlite3.connect(source) as connection:
        sent = connection.execute("SELECT id, key, scope, user_id, value, updated_at FROM settings").fetchall()
    assert {key for _, key, *_ in sent} >= set(config_json)  # the source holds the imported config too

    report = tool.migrate(source, target.url, apply=True)

    assert report.ok and report.committed and report.exit_code == 0, report.problems
    seeded = report.seeded_by_migrations
    assert seeded and seeded.get("settings", 0) >= len(config_json)
    with target.connect() as connection:
        held = _settings(connection)
    assert [row[:5] for row in held] == sorted(tuple(row[:5]) for row in sent)  # the source's rows, nothing else
    assert_same_data(source, target)
    assert {t.table: t.target_rows for t in report.tables}["settings"] == len(sent)


def test_a_dry_run_with_a_config_json_is_clean_and_touches_nothing(config_json, source, target):
    report = tool.migrate(source, target.url, apply=False)

    assert report.ok and report.exit_code == 0 and not report.committed, report.problems
    assert report.seeded_by_migrations == {}
    assert _tables(target) == set()


def test_the_report_says_how_many_seeded_rows_the_source_replaced(config_json, source, target, capsys):
    url = target.url.render_as_string(hide_password=False)

    assert tool.main(["--source", str(source), "--target", url, "--apply"]) == 0

    assert "rows seeded by the migrations (settings: " in capsys.readouterr().out


def test_only_what_the_migrations_always_write_is_reported_as_seeded_without_a_config_json(
    source, target, monkeypatch, tmp_path,
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "no-config-here"))

    report = tool.migrate(source, target.url, apply=True)

    assert report.ok and report.committed
    assert report.seeded_by_migrations == SEEDED


def test_a_target_that_held_data_before_the_run_is_refused_even_if_the_migrations_seeded_it(config_json, source, target):
    """The target already ran the migrations (they seeded ``settings``) before the tool did: that
    is data it held, not something the run created, so it needs ``--replace``."""
    upgrade_to_head(target)
    with target.connect() as connection:
        before = _settings(connection)
    assert before

    refused = tool.migrate(source, target.url, apply=True)

    assert refused.exit_code == 2 and not refused.committed and "already holds data" in refused.problems[0]
    with target.connect() as connection:
        assert _settings(connection) == before
        assert connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 0

    replaced = tool.migrate(source, target.url, apply=True, replace=True)

    assert replaced.ok and replaced.committed, replaced.problems
    assert replaced.seeded_by_migrations == {}  # replaced the data it held, not seeds of this run
    assert_same_data(source, target)


def test_a_failed_verification_with_seeded_rows_rolls_back_to_the_seeded_state(config_json, source, target, monkeypatch):
    real = tool.table_checksum

    def wrong_for_users(connection, table):
        rows, checksum = real(connection, table)
        return rows, checksum + (1 if table.name == "users" else 0)

    monkeypatch.setattr(tool, "table_checksum", wrong_for_users)

    report = tool.migrate(source, target.url, apply=True)

    assert not report.ok and report.exit_code == 1 and not report.committed
    with target.connect() as connection:
        # the migrations' own work stays (the target is at the head), the copy and the emptying do not
        assert connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 0
        assert connection.execute(sa.text("SELECT COUNT(*) FROM settings")).scalar() >= len(config_json)


def test_a_failed_verification_rolls_everything_back(source, target, monkeypatch):
    real = tool.table_checksum

    def wrong_for_users(connection, table):
        rows, checksum = real(connection, table)
        return rows, checksum + (1 if table.name == "users" else 0)

    monkeypatch.setattr(tool, "table_checksum", wrong_for_users)

    report = tool.migrate(source, target.url, apply=True)

    assert not report.ok and report.exit_code == 1 and not report.committed
    assert "rolled back" in report.problems[0]
    with target.connect() as connection:
        for table in tool.APP_TABLES:  # only the migrations' own work stays
            expected = SEEDED.get(table.name, 0)
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == expected, table.name


def test_a_failure_while_copying_rolls_everything_back(source, target):
    # A NUL character cannot be stored in a PostgreSQL text.
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE tracks SET title = 'bad' || char(0) || 'title' WHERE internal_id = 'tracks-2'")

    report = tool.migrate(source, target.url, apply=True)

    assert report.exit_code == 1 and "tracks" in report.problems[0]
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 0


# ---------------------------------------------------------------------------
# SQLite never checked foreign keys
# ---------------------------------------------------------------------------


def _orphans(source: Path) -> None:
    with sqlite3.connect(source) as connection:
        # A download of a deleted user (kept, with no requester), a favorite of a deleted user, and a
        # provider row of a like of a deleted user (which goes with it).
        connection.execute("UPDATE downloads SET requested_by = 'deleted-user' WHERE id = 'downloads-1'")
        connection.execute("UPDATE user_favorites SET user_id = 'deleted-user' WHERE id = 'user_favorites-1'")
        connection.execute("UPDATE user_saved_tracks SET user_id = 'deleted-user' WHERE id = 'user_saved_tracks-1'")


def test_a_reference_that_only_remembers_is_nulled_and_the_other_orphans_stop_the_copy(source, target):
    _orphans(source)

    report = tool.migrate(source, target.url, apply=True)

    assert report.exit_code == 2 and not report.committed
    blocking = {(o.table, o.column) for o in report.orphans if o.action == "blocking"}
    assert blocking == {("user_favorites", "user_id"), ("user_saved_tracks", "user_id")}
    assert any("--discard-orphans" in problem for problem in report.problems)
    assert _tables(target) == set()


def test_discarding_orphans_drops_them_and_what_pointed_at_them(source, target):
    _orphans(source)

    report = tool.migrate(source, target.url, apply=True, discard_orphans=True)

    assert report.ok and report.committed, report.problems
    counts = {t.table: t for t in report.tables}
    assert counts["user_favorites"].discarded == 1 and counts["user_favorites"].copied == ROWS - 1
    assert counts["user_saved_tracks"].discarded == 1
    # the provider row of the discarded like went with it
    assert counts["user_saved_track_providers"].discarded == 1
    assert counts["downloads"].nulled_references == 1 and counts["downloads"].copied == ROWS
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT requested_by FROM downloads WHERE id = 'downloads-1'")).scalar() is None


def test_the_sessions_of_a_deleted_user_are_dropped_without_asking(source, target):
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE refresh_tokens SET user_id = 'deleted-user' WHERE id = 'refresh_tokens-1'")

    report = tool.migrate(source, target.url, apply=True)  # no --discard-orphans

    assert report.ok and report.committed, report.problems
    tokens = {t.table: t for t in report.tables}["refresh_tokens"]
    assert (tokens.source_rows, tokens.discarded, tokens.copied, tokens.target_rows) == (ROWS, 1, ROWS - 1, ROWS - 1)
    assert [(o.table, o.column, o.rows) for o in report.orphans] == [("refresh_tokens", "user_id", 1)]
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM refresh_tokens WHERE id = 'refresh_tokens-1'")).scalar() == 0


def test_the_user_of_an_instance_without_authentication_is_created(source, target):
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE user_favorites SET user_id = ? WHERE id = 'user_favorites-1'", (models.SYSTEM_USER_ID,))

    report = tool.migrate(source, target.url, apply=True)

    assert report.ok and report.committed, report.problems
    users = {t.table: t for t in report.tables}["users"]
    assert users.created == 1
    with target.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": models.SYSTEM_USER_ID}).scalar() == 1
        assert connection.execute(sa.text("SELECT user_id FROM user_favorites WHERE id = 'user_favorites-1'")).scalar() == models.SYSTEM_USER_ID


# ---------------------------------------------------------------------------
# Sequences, the command line
# ---------------------------------------------------------------------------


def test_sequences_continue_after_the_highest_id_copied(target):
    probe = sa.Table("sequence_probe", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), sa.Column("v", sa.String))
    with target.begin() as connection:
        probe.create(connection)
        connection.execute(probe.insert(), [{"id": 1, "v": "a"}, {"id": 7, "v": "b"}, {"id": 50, "v": "c"}])

        assert tool.reset_sequences(connection, [probe]) == ["sequence_probe.id"]

        assert connection.execute(probe.insert().values(v="next").returning(probe.c.id)).scalar() == 51


def test_the_schema_has_no_sequences_to_reset(source, target):
    assert tool.migrate(source, target.url, apply=True).sequences_reset == []


def test_the_command_line_reports_as_text_or_json(source, target, capsys):
    url = target.url.render_as_string(hide_password=False)

    assert tool.main(["--source", str(source), "--target", url]) == 0
    text = capsys.readouterr().out
    assert "Mode: dry-run" in text and "users" in text and "password" not in text

    assert tool.main(["--source", str(source), "--target", url, "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["committed"] and report["mode"] == "apply"
    assert {t["table"] for t in report["tables"]} == {table.name for table in tool.APP_TABLES}


def test_the_target_defaults_to_database_url(source, target, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", target.url.render_as_string(hide_password=False))

    assert tool.main(["--source", str(source)]) == 0
    assert "Mode: dry-run" in capsys.readouterr().out


def test_the_source_file_is_never_changed(source, target):
    stamp = source.stat().st_mtime_ns
    content = source.read_bytes()

    tool.migrate(source, target.url, apply=True)

    assert source.read_bytes() == content and source.stat().st_mtime_ns == stamp


def test_an_empty_source_copies_as_an_empty_database(tmp_path, target):
    empty = sqlite_source(tmp_path, populate=False)

    report = tool.migrate(empty, target.url, apply=True)

    assert report.ok and report.committed
    assert {t.table: t.target_rows for t in report.tables}["users"] == 1  # the system user, as the application keeps it
    assert all(t.target_rows == SEEDED.get(t.table, 0) for t in report.tables if t.table != "users")
