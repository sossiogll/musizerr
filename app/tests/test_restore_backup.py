"""
``scripts/restore_backup.py`` (ADR-0007 §5): putting back the backup ``init`` took before a migration, on the
database under test. A dry run changes nothing; ``--apply`` copies the database as it is first and then replaces
it with the backup.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from db import schema_state
from db.engine import POSTGRESQL, head_revisions
from db.models import User
from scripts import restore_backup
from services.database_init import init_db

from tests.database import DIALECT, migration_database

OLD = "0042_open_mode_accounts"
[HEAD] = head_revisions()  # the head of the day
NOW = datetime(2026, 10, 24, 10, 15, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(
    DIALECT == POSTGRESQL and (shutil.which("pg_dump") is None or shutil.which("pg_restore") is None),
    reason="needs pg_dump and pg_restore (the app image has postgresql-client-17)",
)


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A database migrated from 0042 by ``init`` (so with a backup of 0042 and alice in it), the script pointed at
    it. Returns (engine, the backup)."""
    engine, _config = migration_database(OLD)
    with Session(engine) as session:
        session.add(User(id="u1", username="alice", password_hash="x"))
        session.commit()
    directory = tmp_path / "data" / "backups"
    monkeypatch.setenv("MUSIZEER_DATA_DIR", str(tmp_path / "data"))
    if DIALECT == POSTGRESQL:
        monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    else:
        monkeypatch.setenv("DATABASE_URL", str(engine.url))
    init_db(engine, version="1.5.0", directory=directory, now=lambda: NOW)
    [backup] = list(directory.iterdir())
    # The migrated database moves on: a new user, written after the backup.
    with Session(engine) as session:
        session.add(User(id="u2", username="bob", password_hash="x"))
        session.commit()
    return engine, backup


def _usernames(engine) -> list[str]:
    with Session(engine) as session:
        return sorted(session.execute(sa.select(User.username)).scalars().all())


def test_a_dry_run_checks_the_backup_and_changes_nothing(migrated):
    engine, backup = migrated

    assert restore_backup.main([str(backup)]) == 0

    assert _usernames(engine) == ["alice", "bob"]
    assert schema_state.applied_revisions(engine) == {HEAD}


def test_apply_puts_the_database_back_as_it_was_before_the_migration(migrated, tmp_path):
    engine, backup = migrated

    assert restore_backup.main([str(backup), "--apply"]) == 0

    engine.dispose()
    assert _usernames(engine) == ["alice"], "what was written after the backup is gone"
    assert schema_state.applied_revisions(engine) == {OLD}
    assert "schema_info" not in sa.inspect(engine).get_table_names()


def test_apply_first_keeps_a_copy_of_the_database_as_it_is_and_the_pruning_never_takes_it(migrated, tmp_path):
    engine, backup = migrated

    restore_backup.main([str(backup), "--apply"])

    names = sorted(p.name for p in backup.parent.iterdir())
    [safety] = [n for n in names if n.startswith("pre-restore-")]
    assert backup.name in names and len(names) == 2
    from db import migration_backup

    assert migration_backup.prune_backups(backup.parent, 1) == []
    assert (backup.parent / safety).is_file()


def test_the_restored_database_can_be_migrated_again(migrated, tmp_path):
    engine, backup = migrated
    restore_backup.main([str(backup), "--apply"])
    engine.dispose()

    init_db(engine, version="1.5.1", directory=tmp_path / "again", now=lambda: NOW.replace(day=25))

    assert schema_state.applied_revisions(engine) == {HEAD}
    assert _usernames(engine) == ["alice"]


def test_a_file_of_the_other_engine_or_not_a_backup_is_refused_and_nothing_is_touched(migrated, tmp_path):
    engine, backup = migrated
    other = tmp_path / ("x.sqlite3" if DIALECT == POSTGRESQL else "x.dump")
    other.write_bytes(b"whatever")
    garbage = tmp_path / f"garbage.{backup.suffix.lstrip('.')}"
    garbage.write_bytes(b"not a backup at all")

    assert restore_backup.main([str(other), "--apply"]) == 2
    assert restore_backup.main([str(garbage), "--apply"]) == 2

    assert _usernames(engine) == ["alice", "bob"]
    assert not [p for p in backup.parent.iterdir() if p.name.startswith("pre-restore-")]


def test_a_missing_file_is_refused(migrated, tmp_path):
    _engine, backup = migrated

    assert restore_backup.main([str(Path(tmp_path) / ("nothing" + backup.suffix)), "--apply"]) == 2
