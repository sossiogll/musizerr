"""
Fase 8: verifica della migrazione 0013 (tabella ``routine_runs`` + migrazione
degli intervalli beat pre-esistenti verso le nuove SettingDef dei routine)
su un DB *temporaneo* creato ad hoc — mai sul DB reale. Stesso stile di
``test_migration_0011_drop_user_setting_columns.py``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database


def _insert_system_setting(conn, key: str, value) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, 'system', NULL, :value, :updated_at)"
        ),
        {"id": str(uuid.uuid4()), "key": key, "value": json.dumps(value), "updated_at": datetime.now(timezone.utc)},
    )


def _get_system_value(conn, key: str):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    return json.loads(row[0]) if row is not None else None


@pytest.fixture
def engine_at_0012():
    return migration_database("0012_download_followers_drop_watched_items")


# ---------------------------------------------------------------------------
# routine_runs — nuova tabella
# ---------------------------------------------------------------------------


def test_upgrade_creates_routine_runs_table(engine_at_0012):
    engine, cfg = engine_at_0012
    command.upgrade(cfg, "0013_routine_scheduler")

    with engine.connect() as conn:
        cols = {column["name"] for column in sa.inspect(conn).get_columns("routine_runs")}
    assert cols == {"id", "routine_key", "started_at", "finished_at", "status", "detail"}


def test_downgrade_drops_routine_runs_table(engine_at_0012):
    engine, cfg = engine_at_0012
    command.upgrade(cfg, "0013_routine_scheduler")
    command.downgrade(cfg, "0012_download_followers_drop_watched_items")

    assert "routine_runs" not in sa.inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# Migrazione dei vecchi intervalli (ore->minuti dove serve)
# ---------------------------------------------------------------------------


def test_upgrade_migrates_library_sync_interval_hours_to_minutes(engine_at_0012):
    engine, cfg = engine_at_0012
    with engine.connect() as conn:
        _insert_system_setting(conn, "library_sync_interval_hours", 4)
        conn.commit()

    command.upgrade(cfg, "0013_routine_scheduler")

    with engine.connect() as conn:
        assert _get_system_value(conn, "routine.sync_saved_tracks.interval_minutes") == 240
        assert _get_system_value(conn, "library_sync_interval_hours") is None


def test_upgrade_migrates_delegation_sweep_interval_minutes_unchanged(engine_at_0012):
    engine, cfg = engine_at_0012
    with engine.connect() as conn:
        _insert_system_setting(conn, "delegation_sweep_interval_minutes", 45)
        conn.commit()

    command.upgrade(cfg, "0013_routine_scheduler")

    with engine.connect() as conn:
        assert _get_system_value(conn, "routine.sweep_delegations.interval_minutes") == 45
        assert _get_system_value(conn, "delegation_sweep_interval_minutes") is None


def test_upgrade_is_a_noop_for_a_deployment_that_never_customized_the_old_settings(engine_at_0012):
    """Nessuna riga per le due chiavi vecchie: nulla da migrare, il nuovo
    routine userà semplicemente il default dichiarato nel catalogo (non
    verificabile qui, che opera solo sulla tabella `settings`)."""
    engine, cfg = engine_at_0012

    command.upgrade(cfg, "0013_routine_scheduler")

    with engine.connect() as conn:
        assert _get_system_value(conn, "routine.sync_saved_tracks.interval_minutes") is None
        assert _get_system_value(conn, "routine.sweep_delegations.interval_minutes") is None


def test_downgrade_restores_old_keys_from_routine_settings(engine_at_0012):
    engine, cfg = engine_at_0012
    with engine.connect() as conn:
        _insert_system_setting(conn, "library_sync_interval_hours", 6)
        _insert_system_setting(conn, "delegation_sweep_interval_minutes", 30)
        conn.commit()

    command.upgrade(cfg, "0013_routine_scheduler")
    command.downgrade(cfg, "0012_download_followers_drop_watched_items")

    with engine.connect() as conn:
        assert _get_system_value(conn, "library_sync_interval_hours") == 6
        assert _get_system_value(conn, "delegation_sweep_interval_minutes") == 30
        assert _get_system_value(conn, "routine.sync_saved_tracks.interval_minutes") is None
        assert _get_system_value(conn, "routine.sweep_delegations.interval_minutes") is None


def test_upgrade_then_downgrade_then_reupgrade_is_idempotent(engine_at_0012):
    engine, cfg = engine_at_0012
    with engine.connect() as conn:
        _insert_system_setting(conn, "library_sync_interval_hours", 4)
        conn.commit()

    command.upgrade(cfg, "0013_routine_scheduler")
    command.downgrade(cfg, "0012_download_followers_drop_watched_items")
    command.upgrade(cfg, "0013_routine_scheduler")

    with engine.connect() as conn:
        assert _get_system_value(conn, "routine.sync_saved_tracks.interval_minutes") == 240
