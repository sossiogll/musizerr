"""
Migrazione 0015: la radice della libreria locale passa dall'impostazione
dell'host ``downloads_dir`` a quella dello storage locale
``provider.local.root_dir``. Su un DB temporaneo, mai su quello reale.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

_HOST_KEY = "downloads_dir"
_PROVIDER_KEY = "provider.local.root_dir"



@pytest.fixture
def at_0014():
    return migration_database("0014_library_duplicates")


def _insert(conn, key, value):
    conn.execute(
        sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, 'system', NULL, :value, :updated_at)"
        ),
        {"id": str(uuid.uuid4()), "key": key, "value": json.dumps(value), "updated_at": datetime.now(timezone.utc)},
    )


def _get(conn, key):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE key = :key AND scope = 'system' AND user_id IS NULL"),
        {"key": key},
    ).first()
    return json.loads(row[0]) if row is not None else None


def test_a_stored_downloads_dir_becomes_the_local_storage_root(at_0014):
    engine, cfg = at_0014
    with engine.begin() as conn:
        _insert(conn, _HOST_KEY, "/music")

    command.upgrade(cfg, "0015_local_storage_root")

    with engine.connect() as conn:
        assert _get(conn, _PROVIDER_KEY) == "/music"
        assert _get(conn, _HOST_KEY) is None


def test_without_a_stored_value_nothing_is_written(at_0014):
    engine, cfg = at_0014

    command.upgrade(cfg, "0015_local_storage_root")

    with engine.connect() as conn:
        assert _get(conn, _PROVIDER_KEY) is None


def test_an_existing_root_dir_is_not_overwritten(at_0014):
    engine, cfg = at_0014
    with engine.begin() as conn:
        _insert(conn, _HOST_KEY, "/old")
        _insert(conn, _PROVIDER_KEY, "/kept")

    command.upgrade(cfg, "0015_local_storage_root")

    with engine.connect() as conn:
        assert _get(conn, _PROVIDER_KEY) == "/kept"
        assert _get(conn, _HOST_KEY) is None


def test_downgrade_moves_the_value_back(at_0014):
    engine, cfg = at_0014
    with engine.begin() as conn:
        _insert(conn, _HOST_KEY, "/music")
    command.upgrade(cfg, "0015_local_storage_root")

    command.downgrade(cfg, "0014_library_duplicates")

    with engine.connect() as conn:
        assert _get(conn, _HOST_KEY) == "/music"
        assert _get(conn, _PROVIDER_KEY) is None
