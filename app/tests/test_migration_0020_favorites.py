"""
Migration 0020 and the removal of ``auto_like_on_sync``: every stored row of
the setting is deleted (per user and system), the migration can run again
(downgrade/upgrade) without effect, the ``autolike_reconcile_*`` values move
to their ``like_reconcile_*`` names, one like intent per (user, track) is
kept; no code reads the removed setting, the schema does not list it, and a
row left behind does not break the settings API.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlmodel import Session

from core import settings_catalog as sc
from db.models import SYSTEM_USER_ID, User
from routes.config import get_settings_schema, get_settings_values
from services.config_manager import ConfigManager

from tests.database import migration_database, new_engine, seed_parents
from tests.conftest import FakeRegistry

_APP_ROOT = Path(__file__).resolve().parent.parent
_REMOVED = "auto_like_on_sync"



def _insert_setting(conn, key, value, *, scope="system", user_id=None):
    conn.execute(
        sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, :scope, :user_id, :value, :now)"
        ),
        {"id": str(uuid.uuid4()), "key": key, "scope": scope, "user_id": user_id,
         "value": json.dumps(value), "now": datetime.now(timezone.utc)},
    )


def _settings(conn) -> dict[tuple, object]:
    rows = conn.execute(sa.text("SELECT key, scope, user_id, value FROM settings")).fetchall()
    return {(k, s, u): json.loads(v) for k, s, u, v in rows}


@pytest.fixture
def at_0019():
    engine, cfg = migration_database("0019_host_url_refs")
    with engine.begin() as conn:
        for user in ("u1", "u2"):
            conn.execute(sa.text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :id, 'x', 'user', true, :now)"), {"id": user, "now": datetime.now(timezone.utc)})
        conn.execute(sa.text(
            "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) "
            "VALUES ('t1', 'T', 'A', 'B', 0, :now)"), {"now": datetime.now(timezone.utc)})
    return engine, cfg


def _seed_old_values(conn):
    _insert_setting(conn, _REMOVED, True, scope="user", user_id="u1")
    _insert_setting(conn, _REMOVED, False, scope="user", user_id="u2")
    _insert_setting(conn, _REMOVED, True)  # a stray system row
    _insert_setting(conn, "autolike_reconcile_interval_seconds", 90)
    _insert_setting(conn, "autolike_reconcile_max_age_hours", 12)
    _insert_setting(conn, "registration_open", False)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    for pid, provider, created in (("p1", "sync-a", old), ("p2", "sync-b", datetime.now(timezone.utc))):
        conn.execute(sa.text(
            "INSERT INTO pending_likes (id, user_id, track_id, sync_provider, created_at, attempts) "
            "VALUES (:id, 'u1', 't1', :provider, :created, 0)"), {"id": pid, "provider": provider, "created": created})


def test_upgrade_deletes_the_setting_renames_the_like_settings_and_keeps_one_intent(at_0019):
    engine, cfg = at_0019
    with engine.begin() as conn:
        _seed_old_values(conn)

    command.upgrade(cfg, "0020_favorites")

    with engine.connect() as conn:
        settings = _settings(conn)
        assert not any(key == _REMOVED for key, _, _ in settings)
        assert settings[("like_reconcile_interval_seconds", "system", None)] == 90
        assert settings[("like_reconcile_max_age_hours", "system", None)] == 12
        assert not any(key.startswith("autolike_") for key, _, _ in settings)
        assert settings[("registration_open", "system", None)] is False
        intents = conn.execute(sa.text("SELECT id, sync_provider FROM pending_likes")).fetchall()
        assert [tuple(i) for i in intents] == [("p1", "sync-a")]  # the oldest
        tables = sa.inspect(conn).get_table_names()
        assert "user_favorites" in tables
        columns = {c["name"] for c in sa.inspect(conn).get_columns("user_saved_tracks")}
        assert {"library_track_id", "matched_at"} <= columns


def test_the_migration_is_idempotent(at_0019):
    engine, cfg = at_0019
    with engine.begin() as conn:
        _seed_old_values(conn)
    command.upgrade(cfg, "0020_favorites")
    with engine.connect() as conn:
        first = _settings(conn)

    command.downgrade(cfg, "0019_host_url_refs")
    with engine.connect() as conn:
        # The downgrade restores the old names and never recreates the removed setting.
        downgraded = _settings(conn)
        assert ("autolike_reconcile_interval_seconds", "system", None) in downgraded
        assert not any(key == _REMOVED for key, _, _ in downgraded)
    command.upgrade(cfg, "0020_favorites")

    with engine.connect() as conn:
        assert _settings(conn) == first


def test_a_new_intent_needs_no_sync_provider_and_is_unique_per_user_and_track(at_0019):
    engine, cfg = at_0019
    command.upgrade(cfg, "0020_favorites")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO pending_likes (id, user_id, track_id, sync_provider, created_at, attempts) "
            "VALUES ('n1', 'u1', 't1', NULL, :now, 0)"), {"now": now})
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO pending_likes (id, user_id, track_id, sync_provider, created_at, attempts) "
                "VALUES ('n2', 'u1', 't1', 'sync', :now, 0)"), {"now": now})


def test_the_schema_no_longer_lists_the_setting():
    assert _REMOVED not in {s.key for s in sc.CATALOG}
    assert sc.get_def(_REMOVED) is None


def test_no_code_path_reads_the_setting():
    """Only the migrations (and this test) may still name it."""
    offenders = []
    for path in _APP_ROOT.rglob("*.py"):
        relative = path.relative_to(_APP_ROOT)
        if relative.parts[0] in ("migrations", "tests"):
            continue
        text = path.read_text(encoding="utf-8")
        if _REMOVED in text or "autolike_reconcile" in text:
            offenders.append(str(relative))
    assert offenders == []


def test_a_row_left_behind_does_not_break_the_settings_api():
    engine = new_engine()
    with Session(engine) as session:
        seed_parents(session, users=["someone"])
    with engine.begin() as conn:
        _insert_setting(conn, _REMOVED, True, scope="user", user_id="someone")
        _insert_setting(conn, _REMOVED, True)
    config = ConfigManager(engine=engine)
    admin = User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin", is_active=True)

    with Session(engine) as session:
        schema = get_settings_schema(config, FakeRegistry(), session, admin)
        values = get_settings_values(config, FakeRegistry(), session, admin)

    assert _REMOVED not in {entry["key"] for entry in schema["settings"]}
    assert _REMOVED not in values
