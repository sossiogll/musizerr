"""
The database guard (ADR-0007 §4), on the database under test (SQLite or PostgreSQL): the backup ``init`` takes
before it migrates (only when something is pending, the last N kept, a failure stops the migration),
``schema_info``, and the start check of the API and the workers.

The backups are real: SQLite's online backup, and ``pg_dump`` on PostgreSQL (the image ships the client of the
server's major; a runner without one skips the PostgreSQL ones).
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from core import schema_guard as guard
from db import migration_backup, schema_state
from db.engine import POSTGRESQL, alembic_config, head_revisions, upgrade_to_head
from db.models import SchemaInfo, User
from services import database_init
from services.database_init import SchemaError, init_db, require_current_schema
from workers import celery_app

from tests.database import DIALECT, migration_database, new_engine

OLD = "0042_open_mode_accounts"
GUARD = "0043_schema_info"  # the migration that creates schema_info
[HEAD] = head_revisions()  # the head of the day: a later migration moves it, and the tests with it
NOW = datetime(2026, 10, 24, 10, 15, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(
    DIALECT == POSTGRESQL and shutil.which("pg_dump") is None,
    reason="the PostgreSQL backup needs pg_dump (the app image has postgresql-client-17)",
)


@pytest.fixture(autouse=True)
def _backup_settings(monkeypatch):
    monkeypatch.delenv("MUSIZEER_MIGRATION_BACKUP", raising=False)
    monkeypatch.delenv("MUSIZEER_MIGRATION_BACKUPS_KEEP", raising=False)
    # A MAJOR that names an oldest version to upgrade from must not break the flows below.
    monkeypatch.setattr(guard, "MIN_UPGRADE_FROM", None)


def _expected_after(applied: frozenset[str], engine):
    """What a run that starts at ``applied`` leaves in schema_info, from the phases the migrations declare (so a
    later migration of either phase does not break the tests that migrate from 0042)."""
    catalog = schema_state.migrations(engine)
    contract = any(m.phase == guard.PHASE_CONTRACT for m in schema_state.pending(applied, catalog))
    return {
        "phase": guard.PHASE_CONTRACT if contract else guard.PHASE_EXPAND,
        "min_app_version": "1.0.0" if contract else None,   # for an app of version 1.x
        "contract_revision": schema_state.newest_contract(schema_state.code_heads(engine), catalog),
    }


@pytest.fixture
def backups(tmp_path) -> Path:
    return tmp_path / "backups"


def _head_of_the_previous_release():
    """A database as the release before the guard left it: at 0042, with a user in it."""
    engine, _config = migration_database(OLD)
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
    return engine


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def _revisions(engine) -> set[str]:
    return set(schema_state.applied_revisions(engine))


def _row(engine) -> SchemaInfo:
    with Session(engine) as session:
        row = session.get(SchemaInfo, "schema")
        assert row is not None, "schema_info has no row"
        return row


# ---------------------------------------------------------------------------
# The backup before a migration
# ---------------------------------------------------------------------------


def test_a_pending_migration_is_backed_up_first_with_the_revisions_in_the_name(backups):
    engine = _head_of_the_previous_release()

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    extension = "dump" if DIALECT == POSTGRESQL else "sqlite3"
    assert _files(backups) == [f"premigration-20261024T101500Z-from-{OLD}-to-{HEAD}.{extension}"]
    assert _revisions(engine) == {HEAD}


def test_the_backup_holds_the_database_as_it_was_before_the_migration(backups):
    engine = _head_of_the_previous_release()

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    [backup] = [p for p in backups.iterdir()]
    if DIALECT == POSTGRESQL:
        dumped = subprocess.run(
            ["pg_restore", "-f", "-", "--data-only", "-t", "users", str(backup)],
            capture_output=True, text=True, check=True,
        ).stdout
        listing = subprocess.run(["pg_restore", "--list", str(backup)], capture_output=True, text=True, check=True).stdout
        assert "alice" in dumped
        assert "schema_info" not in listing, "the dump is from before the migration that creates schema_info"
    else:
        import sqlite3

        copy = sqlite3.connect(backup)
        try:
            assert copy.execute("SELECT username FROM users").fetchall() == [("alice",)]
            tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            copy.close()
        assert "schema_info" not in tables and "alembic_version" in tables


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_backup_is_private_to_its_owner(backups):
    engine = _head_of_the_previous_release()

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    [backup] = list(backups.iterdir())
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700


def test_an_up_to_date_database_takes_no_backup_and_changes_nothing(backups):
    engine = _head_of_the_previous_release()
    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)
    first = _row(engine)
    before = _files(backups)

    verdict = init_db(engine, version="1.6.0", directory=backups, now=lambda: NOW.replace(day=25))

    assert verdict.status == guard.CURRENT
    assert _files(backups) == before
    after = _row(engine)
    assert (after.app_version, after.migrated_at) == (first.app_version, first.migrated_at), "a no-op start records nothing"


def test_a_new_empty_database_is_migrated_without_a_backup(backups):
    engine = new_engine(schema=False, addressable=True)

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert not backups.exists() or _files(backups) == []
    assert _revisions(engine) == set(schema_state.code_heads(engine))


@pytest.mark.sqlite_only("a SQLite database that exists from before migrations were introduced")
def test_a_database_from_before_alembic_is_backed_up_as_unversioned_then_stamped_and_migrated(backups):
    engine, _config = migration_database("0001")  # the baseline tables, then the history is forgotten
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE alembic_version"))

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    [name] = _files(backups)
    assert guard.parse_backup_name(name).source == guard.UNVERSIONED
    assert _revisions(engine) == set(schema_state.code_heads(engine))


def test_only_the_last_five_backups_are_kept_and_what_is_not_ours_stays(backups):
    engine = _head_of_the_previous_release()
    backups.mkdir()
    for day in range(1, 8):  # seven earlier backups, days 1..7 of the month
        (backups / guard.backup_name(datetime(2026, 9, day, tzinfo=timezone.utc), OLD, OLD, "dump")).write_bytes(b"x")
    (backups / "notes.txt").write_text("mine")

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    kept = [n for n in _files(backups) if n != "notes.txt"]
    assert len(kept) == 5
    assert kept[-1].startswith("premigration-20261024T101500Z"), "the new one is among them"
    assert [guard.parse_backup_name(n).stamp[:8] for n in kept[:-1]] == ["20260904", "20260905", "20260906", "20260907"]
    assert "notes.txt" in _files(backups)


def test_pruning_also_removes_a_half_written_file_a_crash_left_and_only_that(backups):
    backups.mkdir()
    half = backups / (guard.backup_name(NOW, OLD, HEAD, "dump") + migration_backup.PARTIAL_SUFFIX)
    half.write_bytes(b"half")
    mine = backups / "notes.txt"
    mine.write_text("mine")
    real = backups / guard.backup_name(NOW, OLD, HEAD, "dump")
    real.write_bytes(b"x")

    assert migration_backup.prune_backups(backups, 5) == []

    assert not half.exists() and mine.exists() and real.exists()
    assert migration_backup.list_backups(backups) == [real.name]


def test_the_number_kept_is_configurable_and_never_below_one(backups, monkeypatch):
    monkeypatch.setenv("MUSIZEER_MIGRATION_BACKUPS_KEEP", "2")
    engine = _head_of_the_previous_release()
    backups.mkdir()
    for day in range(1, 4):
        (backups / guard.backup_name(datetime(2026, 9, day, tzinfo=timezone.utc), OLD, OLD, "dump")).write_bytes(b"x")

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert len(_files(backups)) == 2

    monkeypatch.setenv("MUSIZEER_MIGRATION_BACKUPS_KEEP", "0")
    assert database_init.backups_keep() == 1
    monkeypatch.setenv("MUSIZEER_MIGRATION_BACKUPS_KEEP", "many")
    assert database_init.backups_keep() == database_init.DEFAULT_BACKUPS_KEEP
    monkeypatch.delenv("MUSIZEER_MIGRATION_BACKUPS_KEEP")
    assert database_init.backups_keep() == 5


def test_a_failed_backup_stops_the_migration_with_a_message_that_says_why(backups, monkeypatch):
    engine = _head_of_the_previous_release()

    def broken(*args, **kwargs):
        raise migration_backup.BackupError("the disk is full")

    monkeypatch.setattr(migration_backup, "backup_database", broken)

    with pytest.raises(SchemaError) as stopped:
        init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert "NOT migrated" in str(stopped.value) and "the disk is full" in str(stopped.value)
    assert "MUSIZEER_MIGRATION_BACKUP=off" in str(stopped.value)
    assert _revisions(engine) == {OLD}, "the database was not touched"
    assert "schema_info" not in sa.inspect(engine).get_table_names()


def test_a_backup_folder_that_cannot_be_created_stops_the_migration(tmp_path):
    engine = _head_of_the_previous_release()
    in_the_way = tmp_path / "backups"
    in_the_way.write_text("a file where the folder should be")

    with pytest.raises(SchemaError, match="NOT migrated"):
        init_db(engine, version="1.5.0", directory=in_the_way, now=lambda: NOW)

    assert _revisions(engine) == {OLD}


def test_a_failed_backup_leaves_no_file_that_looks_like_a_backup(backups, monkeypatch):
    engine = _head_of_the_previous_release()
    if DIALECT == POSTGRESQL:
        monkeypatch.setattr(migration_backup, "_tool", lambda name: "/bin/false")
    else:
        def broken(path, destination):
            destination.write_bytes(b"half a file")
            raise OSError("no space left on device")

        monkeypatch.setattr(migration_backup, "_dump_sqlite", broken)

    with pytest.raises(SchemaError, match="NOT migrated"):
        init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert _files(backups) == []


@pytest.mark.postgresql_only("pg_dump refuses an older client against a newer server")
def test_pg_dump_failing_says_what_it_said_and_never_shows_the_password(backups, monkeypatch):
    engine = _head_of_the_previous_release()
    password = engine.url.password
    bad = engine.url.set(host="127.0.0.1", port=1)  # nothing listens here

    with pytest.raises(migration_backup.BackupError) as failed:
        migration_backup.backup_database(bad, backups, source=OLD, target=HEAD, now=NOW)

    assert "pg_dump failed" in str(failed.value)
    assert not password or password not in str(failed.value)
    assert _files(backups) == []


def test_the_backup_can_be_switched_off_for_whoever_backs_up_otherwise(backups, monkeypatch, caplog):
    monkeypatch.setenv("MUSIZEER_MIGRATION_BACKUP", "off")
    engine = _head_of_the_previous_release()

    with caplog.at_level(logging.WARNING):
        init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert not backups.exists()
    assert _revisions(engine) == {HEAD}
    assert "without a backup" in caplog.text


def test_a_migration_that_fails_after_the_backup_says_where_the_backup_is(backups, monkeypatch, caplog):
    engine = _head_of_the_previous_release()

    def failing(db_engine):
        raise RuntimeError("the migration blew up")

    monkeypatch.setattr(database_init, "upgrade_to_head", failing)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="blew up"):
        init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    [backup] = list(backups.iterdir())
    assert str(backup) in caplog.text
    assert "schema_info" not in sa.inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# schema_info
# ---------------------------------------------------------------------------


def test_schema_info_records_who_migrated_where_and_what_can_run_on_it(backups):
    engine = _head_of_the_previous_release()

    init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    row = _row(engine)
    expected = _expected_after(frozenset({OLD}), engine)
    assert row.revision == HEAD
    assert row.app_version == "1.5.0"
    assert (row.phase, row.min_app_version, row.contract_revision) == (
        expected["phase"], expected["min_app_version"], expected["contract_revision"],
    )
    assert row.migrated_at is not None


def test_a_database_migrated_from_empty_by_a_release_records_its_major_as_the_minimum(backups):
    engine = new_engine(schema=False, addressable=True)

    init_db(engine, version="2.3.1+4.abc1234", directory=backups, now=lambda: NOW)

    row = _row(engine)
    assert row.phase == "contract", "the history holds contract migrations and this run applied them"
    assert row.min_app_version == "2.0.0"
    assert row.app_version == "2.3.1+4.abc1234"


def test_a_development_build_records_its_name_and_raises_no_minimum(backups):
    engine = new_engine(schema=False, addressable=True)

    init_db(engine, version="dev", directory=backups, now=lambda: NOW)

    row = _row(engine)
    assert (row.app_version, row.min_app_version) == ("dev", None)
    assert row.contract_revision == _expected_after(frozenset(), engine)["contract_revision"]
    assert row.contract_revision is not None


def test_an_up_to_date_database_with_no_row_gets_one(backups):
    engine = new_engine(schema=False, addressable=True)
    upgrade_to_head(engine)  # migrated by hand (alembic upgrade head): no row
    assert schema_state.read_record(engine) is None

    verdict = init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert verdict.status == guard.CURRENT
    assert _row(engine).app_version == "1.5.0"
    assert not backups.exists() or _files(backups) == []


def test_the_row_is_replaced_not_duplicated(backups):
    engine = new_engine(schema=False, addressable=True)
    upgrade_to_head(engine)
    record = guard.SchemaRecord("a", "1.0.0", None, None, "expand")
    schema_state.write_record(engine, record)
    schema_state.write_record(engine, guard.SchemaRecord("b", "1.1.0", "1.0.0", "x", "contract"))

    with Session(engine) as session:
        rows = session.scalars(sa.select(SchemaInfo)).all()
    assert [(r.revision, r.app_version, r.min_app_version, r.contract_revision, r.phase) for r in rows] == [
        ("b", "1.1.0", "1.0.0", "x", "contract"),
    ]
    assert schema_state.read_record(engine).revision == "b"


def test_the_upgrade_path_is_checked_before_anything_is_touched(backups, monkeypatch):
    engine = _head_of_the_previous_release()
    monkeypatch.setattr(guard, "MIN_UPGRADE_FROM", "1.9.0")

    with pytest.raises(SchemaError, match="MUSIZEER_VERSION=1.9.0"):
        init_db(engine, version="2.0.0", directory=backups, now=lambda: NOW)

    assert _revisions(engine) == {OLD} and not backups.exists()


def test_a_new_database_has_no_upgrade_path_to_go_through(backups, monkeypatch):
    engine = new_engine(schema=False, addressable=True)
    monkeypatch.setattr(guard, "MIN_UPGRADE_FROM", "1.9.0")

    init_db(engine, version="2.0.0", directory=backups, now=lambda: NOW)

    assert _revisions(engine) == set(schema_state.code_heads(engine))


# ---------------------------------------------------------------------------
# The start check (the API and the workers)
# ---------------------------------------------------------------------------


def _migrated(backups, version="1.5.0"):
    engine = _head_of_the_previous_release()
    init_db(engine, version=version, directory=backups, now=lambda: NOW)
    return engine


def _pretend_a_later_release_migrated(engine, *, revision, contract=None, minimum=None):
    """What the database looks like after a later release's init: the revision it is at, in alembic_version and
    in schema_info (its code is not here, so its revision is one this code does not know)."""
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM alembic_version"))
        connection.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision})
    row = _row(engine)
    schema_state.write_record(engine, guard.SchemaRecord(
        revision=revision, app_version="1.6.0", min_app_version=minimum or row.min_app_version,
        contract_revision=contract or row.contract_revision, phase="contract" if contract else "expand",
    ))


def test_the_start_check_accepts_a_database_that_is_up_to_date(backups):
    engine = _migrated(backups)

    assert require_current_schema(engine, version="1.5.0", directory=backups).status == guard.CURRENT


def test_the_start_check_refuses_a_new_empty_database_and_says_init_has_not_run(backups):
    engine = new_engine(schema=False, addressable=True)

    with pytest.raises(SchemaError, match="init"):
        require_current_schema(engine, version="1.5.0", directory=backups)


def test_the_start_check_refuses_a_database_behind_the_code(backups):
    engine = _head_of_the_previous_release()

    with pytest.raises(SchemaError) as behind:
        require_current_schema(engine, version="1.5.0", directory=backups)

    assert OLD in str(behind.value) and HEAD in str(behind.value) and "scripts.init_db" in str(behind.value)
    assert _revisions(engine) == {OLD}, "checking migrates nothing"


def test_the_start_check_accepts_the_rollback_over_expand_only_migrations(backups, caplog):
    engine = _migrated(backups)
    _pretend_a_later_release_migrated(engine, revision="0050_a_later_expand")

    with caplog.at_level(logging.WARNING):
        verdict = require_current_schema(engine, version="1.5.0", directory=backups)

    assert verdict.status == guard.ROLLBACK
    assert "0050_a_later_expand" in caplog.text


def test_init_leaves_the_database_of_a_later_release_as_it_is_and_takes_no_backup(backups):
    engine = _migrated(backups)
    _pretend_a_later_release_migrated(engine, revision="0050_a_later_expand")
    before = _files(backups)

    verdict = init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW.replace(day=26))

    assert verdict.status == guard.ROLLBACK
    assert _revisions(engine) == {"0050_a_later_expand"}
    assert _files(backups) == before
    assert _row(engine).app_version == "1.6.0", "the row of the release that migrated it is not overwritten"


def test_the_start_check_refuses_a_database_a_later_release_migrated_with_a_contract_migration(backups):
    engine = _migrated(backups)
    _pretend_a_later_release_migrated(engine, revision="0100_drop_something", contract="0100_drop_something",
                                      minimum="2.0.0")

    with pytest.raises(SchemaError) as refused:
        require_current_schema(engine, version="1.5.0", directory=backups)

    message = str(refused.value)
    assert "0100_drop_something" in message
    assert "Musizeer 2.0.0 or later" in message
    assert "premigration-" in message, "it names the backup to go back with"
    assert f"from-{OLD}-to-{HEAD}" in message


def test_init_refuses_it_too_and_migrates_nothing(backups):
    engine = _migrated(backups)
    _pretend_a_later_release_migrated(engine, revision="0100_drop_something", contract="0100_drop_something",
                                      minimum="2.0.0")

    with pytest.raises(SchemaError, match="cannot run on this database"):
        init_db(engine, version="1.5.0", directory=backups, now=lambda: NOW)

    assert _revisions(engine) == {"0100_drop_something"}


def test_the_start_check_refuses_a_later_database_that_has_no_schema_info(backups):
    engine = _migrated(backups)
    _pretend_a_later_release_migrated(engine, revision="0050_unknown")
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM schema_info"))

    with pytest.raises(SchemaError, match="schema_info"):
        require_current_schema(engine, version="1.5.0", directory=backups)


def test_a_workers_start_refuses_what_the_api_refuses_and_exits(monkeypatch, caplog):
    monkeypatch.setattr(
        database_init, "require_current_schema",
        lambda: (_ for _ in ()).throw(SchemaError("the database is behind: run init")),
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as exited:
        celery_app._require_schema()  # noqa: SLF001

    assert exited.value.code == 1
    assert "run init" in caplog.text


def test_the_api_does_not_start_on_a_schema_it_cannot_use_and_runs_nothing_after_the_check(monkeypatch, caplog):
    import main
    from fastapi.testclient import TestClient

    from tests.credentials_support import KEY, use_keys

    ran: list[str] = []

    def refuse():
        raise SchemaError("the database is behind: run init")

    monkeypatch.setattr(main, "require_current_schema", refuse)
    monkeypatch.setattr(main, "init_singletons", lambda: ran.append("singletons"))
    monkeypatch.setattr(main, "_ensure_system_user", lambda: ran.append("system user"))
    monkeypatch.setattr(main, "_bootstrap_admin", lambda: ran.append("admin bootstrap"))
    use_keys(monkeypatch, KEY)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SchemaError, match="run init"):
        with TestClient(main.app):
            pass

    assert ran == []
    assert "run init" in caplog.text


def test_a_workers_start_goes_on_when_the_schema_is_usable(monkeypatch):
    monkeypatch.setattr(database_init, "require_current_schema", lambda: guard.Verdict(guard.CURRENT, "ok"))

    celery_app._require_schema()  # noqa: SLF001


# ---------------------------------------------------------------------------
# The migrations declare their phase
# ---------------------------------------------------------------------------


def test_every_migration_declares_its_phase_expand_or_contract():
    engine = new_engine(schema=False)
    catalog = schema_state.migrations(engine)

    assert catalog, "no migrations were found"
    assert schema_state.undeclared(catalog) == []
    assert all(m.phase in guard.PHASES for m in catalog.values())


def test_a_migration_with_no_phase_or_a_wrong_one_is_reported():
    catalog = {
        "a": schema_state.Migration("a", "expand", False, ()),
        "b": schema_state.Migration("b", None, False, ("a",)),
        "c": schema_state.Migration("c", "contrcat", False, ("b",)),
    }

    assert schema_state.undeclared(catalog) == ["b", "c"]


def test_the_history_is_one_chain_and_the_guard_migration_is_in_it():
    engine = new_engine(schema=False)
    catalog = schema_state.migrations(engine)

    assert schema_state.code_heads(engine) == {HEAD}, "one head: a second would need a merge"
    assert GUARD in schema_state.ancestors(frozenset({HEAD}), catalog)


def test_no_migration_that_drops_or_renames_a_column_or_table_is_marked_expand():
    """The rule of ADR-0007 §4, read from the source: what removes or renames a column or a table is contract."""
    import ast

    engine = new_engine(schema=False)
    versions = Path(alembic_config(engine.url).get_main_option("script_location")) / "versions"
    catalog = schema_state.migrations(engine)
    removing = {"drop_column", "drop_table", "rename_table"}
    offenders = []
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text())
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        for call in (n for n in ast.walk(upgrade) if isinstance(n, ast.Call)):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            renames = name == "alter_column" and any(k.arg == "new_column_name" for k in call.keywords)
            if name in removing or renames:
                revision = next(
                    n.value.value for n in tree.body
                    if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "revision"
                )
                if catalog[revision].phase != guard.PHASE_CONTRACT:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_irreversible_is_a_flag_not_a_phase():
    catalog = schema_state.migrations(new_engine(schema=False))
    irreversible = sorted(m.revision for m in catalog.values() if m.irreversible)

    # The ones whose downgrade does nothing or may refuse (ADR-0007 context), and the one that came after.
    assert set(irreversible) >= {
        "0016_drop_anonymous_downloads", "0017_retry_unidentified_fingerprints", "0036_linked_accounts",
        "0040_storage_provider_explicit", "0041_encrypt_credentials", "0042_open_mode_accounts",
    }
    assert all(isinstance(m.irreversible, bool) for m in catalog.values())


def test_the_pending_migrations_of_a_database_are_the_ones_above_it_oldest_first():
    engine = new_engine(schema=False)
    catalog = schema_state.migrations(engine)

    above = schema_state.pending(frozenset({OLD}), catalog)
    assert above and above[0].revision == GUARD and above[-1].revision == HEAD
    assert schema_state.pending(frozenset({HEAD}), catalog) == []
    everything = schema_state.pending(frozenset(), catalog)
    assert len(everything) == len(catalog) and everything[0].revision == "0001" and everything[-1].revision == HEAD


def test_the_newest_contract_migration_of_a_history_is_found_from_the_revisions_applied():
    catalog = schema_state.migrations(new_engine(schema=False))

    assert schema_state.newest_contract(frozenset({OLD}), catalog) == OLD
    assert schema_state.newest_contract(frozenset({GUARD}), catalog) == OLD
    assert schema_state.newest_contract(frozenset({"0035_download_requests"}), catalog) == "0032_concurrency_constraints"
    assert schema_state.newest_contract(frozenset({"0005"}), catalog) is None
