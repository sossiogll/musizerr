"""
Migrations 0045 and 0046 (ADR-0008). 0045 adds ``root_folders`` and ``host_state`` (expand). 0046 sets the stored working
folder aside for the conversion that runs when the gateway answers (contract): it records the old value, or the old
default when the install has been used, and removes the row the new code could not read. Run on SQLite and on PostgreSQL
alike (the suite's dialect).
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.credentials_support import raw, raw_scalar
from tests.database import migration_database

PREVIOUS, ROOTS, HEAD = "0044_rename_duplicate_origin", "0045_root_folders", "0046_working_folder_legacy"
NOW = datetime(2026, 10, 25, 12, 0)
KEY = "legacy_workspace_dir"


@pytest.fixture
def db():
    return migration_database(PREVIOUS)


def _put_setting(engine, value, key="workspace_dir") -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO settings (id, key, scope, user_id, value, updated_at) VALUES (:id, :key, 'system', NULL, :value, :now)"),
            {"id": f"id-{key}", "key": key, "value": json.dumps(value), "now": NOW},
        )


def _setting(engine, key="workspace_dir"):
    rows = raw(engine, f"SELECT value FROM settings WHERE key = '{key}' AND scope = 'system'")
    return json.loads(rows[0][0]) if rows else None


def _legacy(engine):
    rows = raw(engine, f"SELECT value FROM host_state WHERE key = '{KEY}'")
    return json.loads(rows[0][0]) if rows else None


def _used(engine) -> None:
    """The install has been used: a track exists."""
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) VALUES ('t1', 'a', 'b', 'c', 1, :now)"),
            {"now": NOW},
        )


# ------------------------------------------------------------------
# 0045
# ------------------------------------------------------------------


def test_0045_adds_the_two_tables_and_nothing_else(db):
    engine, cfg = db

    command.upgrade(cfg, ROOTS)

    inspector = sa.inspect(engine)
    assert {"root_folders", "host_state"} <= set(inspector.get_table_names())
    assert [c["name"] for c in inspector.get_columns("root_folders")] == [
        "id", "label", "path", "state", "reason", "checked_at", "created_at", "updated_at",
    ]
    assert [c["name"] for c in inspector.get_columns("host_state")] == ["key", "value", "updated_at"]
    assert raw_scalar(engine, "SELECT version_num FROM alembic_version") == ROOTS


def test_0045_makes_the_path_of_a_root_unique(db):
    engine, cfg = db
    command.upgrade(cfg, ROOTS)
    insert = (
        "INSERT INTO root_folders (id, label, path, state, reason, created_at, updated_at) "
        "VALUES (:id, 'L', '/library', 'accepted', '', :now, :now)"
    )
    with engine.begin() as conn:
        conn.execute(sa.text(insert), {"id": "a", "now": NOW})

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(insert), {"id": "b", "now": NOW})


def test_0045_downgrades_by_dropping_the_tables(db):
    engine, cfg = db
    command.upgrade(cfg, ROOTS)

    command.downgrade(cfg, PREVIOUS)

    assert not {"root_folders", "host_state"} & set(sa.inspect(engine).get_table_names())


# ------------------------------------------------------------------
# 0046
# ------------------------------------------------------------------


def test_0046_sets_a_stored_working_folder_aside_and_removes_the_row(db):
    engine, cfg = db
    _put_setting(engine, "/data/work")

    command.upgrade(cfg, HEAD)

    assert _setting(engine) is None, "the new code never finds a text under a folder setting"
    record = _legacy(engine)
    assert record["value"] == "/data/work" and record["default"].startswith("/downloads/") and record["old_leaf"]
    assert record["default"].endswith(record["old_leaf"])


def test_0046_on_a_used_install_with_no_stored_value_records_the_old_default(db):
    engine, cfg = db
    _used(engine)

    command.upgrade(cfg, HEAD)

    record = _legacy(engine)
    assert record["value"] is None and record["default"] == "/downloads/" + record["old_leaf"]


def test_0046_on_a_fresh_install_records_nothing(db):
    engine, cfg = db

    command.upgrade(cfg, HEAD)

    assert _legacy(engine) is None and raw_scalar(engine, "SELECT version_num FROM alembic_version") == HEAD


@pytest.mark.parametrize("stored", ["", "   ", 5, None, ["x"]])
def test_0046_ignores_a_stored_value_that_is_not_a_path_but_still_removes_it(db, stored):
    engine, cfg = db
    _put_setting(engine, stored)

    command.upgrade(cfg, HEAD)

    assert _setting(engine) is None and _legacy(engine)["value"] is None


def test_0046_touches_no_other_setting(db):
    engine, cfg = db
    _put_setting(engine, "/data/work")
    _put_setting(engine, "/library", key="provider.local.root_dir")
    _put_setting(engine, "x", key="save_cover")

    command.upgrade(cfg, HEAD)

    assert _setting(engine, "provider.local.root_dir") == "/library" and _setting(engine, "save_cover") == "x"


def test_running_0046_twice_keeps_one_record(db):
    engine, cfg = db
    _put_setting(engine, "/data/work")
    command.upgrade(cfg, HEAD)
    first = _legacy(engine)

    command.stamp(cfg, ROOTS)
    command.upgrade(cfg, HEAD)

    assert _legacy(engine)["default"] == first["default"]
    assert raw_scalar(engine, "SELECT COUNT(*) FROM host_state") == 1


def test_the_downgrade_of_0046_writes_the_stored_value_back(db):
    engine, cfg = db
    _put_setting(engine, "/data/work")
    command.upgrade(cfg, HEAD)

    command.downgrade(cfg, ROOTS)

    assert _setting(engine) == "/data/work" and _legacy(engine) is None


def test_the_downgrade_of_0046_writes_the_old_default_back_when_there_was_no_value(db):
    engine, cfg = db
    _used(engine)
    command.upgrade(cfg, HEAD)
    old_default = _legacy(engine)["default"]

    command.downgrade(cfg, ROOTS)

    assert _setting(engine) == old_default


def test_the_downgrade_of_0046_keeps_a_value_set_since(db):
    engine, cfg = db
    _put_setting(engine, "/data/work")
    command.upgrade(cfg, HEAD)
    _put_setting(engine, "/chosen")

    command.downgrade(cfg, ROOTS)

    assert _setting(engine) == "/chosen"


def test_the_phases_are_declared_honestly():
    from migrations.versions import __path__ as _  # noqa: F401  (the package exists)
    import importlib.util
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    phases = {}
    for name in ("0045_root_folders", "0046_working_folder_legacy"):
        spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        phases[name] = module.phase

    assert phases == {"0045_root_folders": "expand", "0046_working_folder_legacy": "contract"}
