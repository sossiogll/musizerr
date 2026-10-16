"""
Migrazione 0016 e impostazioni dopo la rimozione di
``allow_anonymous_downloads``: le righe salvate vengono cancellate, e anche
una riga rimasta (prima della migrazione) non rompe né l'avvio né l'API
delle impostazioni, e non compare nello schema.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from sqlmodel import Session

from db.models import SYSTEM_USER_ID, User
from routes.config import get_settings_schema, get_settings_values
from services.config_manager import ConfigManager

from tests.database import migration_database, new_engine
from tests.conftest import FakeRegistry

_KEY = "allow_anonymous_downloads"



def _insert(conn, key, value, *, scope="system", user_id=None):
    conn.execute(
        sa.text(
            "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
            "VALUES (:id, :key, :scope, :user_id, :value, :now)"
        ),
        {
            "id": str(uuid.uuid4()), "key": key, "scope": scope, "user_id": user_id,
            "value": json.dumps(value), "now": datetime.now(timezone.utc),
        },
    )


def _count(conn, key) -> int:
    return conn.execute(sa.text("SELECT COUNT(*) FROM settings WHERE key = :key"), {"key": key}).scalar()


@pytest.fixture
def at_0015():
    return migration_database("0015_local_storage_root")


def test_upgrade_removes_every_stored_value(at_0015):
    engine, cfg = at_0015
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
            "VALUES ('someone', 'someone', 'x', 'user', true, :now)"), {"now": datetime.now(timezone.utc)})
        _insert(conn, _KEY, True)
        _insert(conn, _KEY, True, scope="user", user_id="someone")
        _insert(conn, "registration_open", False)

    command.upgrade(cfg, "0016_drop_anonymous_downloads")

    with engine.connect() as conn:
        assert _count(conn, _KEY) == 0
        assert _count(conn, "registration_open") == 1


def test_downgrade_is_a_no_op(at_0015):
    engine, cfg = at_0015
    command.upgrade(cfg, "0016_drop_anonymous_downloads")

    command.downgrade(cfg, "0015_local_storage_root")

    with engine.connect() as conn:
        assert _count(conn, _KEY) == 0


@pytest.fixture
def stale_store():
    engine = new_engine()
    with engine.begin() as conn:
        _insert(conn, _KEY, True)
    return engine


def test_settings_api_works_with_a_stale_stored_value_and_hides_the_key(stale_store):
    config = ConfigManager(engine=stale_store)
    admin = User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin", is_active=True)

    with Session(stale_store) as session:
        schema = get_settings_schema(config, FakeRegistry(), session, admin)
        values = get_settings_values(config, FakeRegistry(), session, admin)

    keys = {entry["key"] for entry in schema["settings"]}
    assert "auth_enabled" in keys
    assert _KEY not in keys
    assert _KEY not in values
