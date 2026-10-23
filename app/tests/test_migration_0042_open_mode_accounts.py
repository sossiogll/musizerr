"""
Migration 0042: the accounts an older version wrote under the synthetic user
(``__system__``, authentication off) become the shared ones, because nobody
reads that user's own row any more (``services/credentials.py``). When the
instance also had a shared one, the most recently written wins; only the
OAuth2 token and the login are moved; a real user's rows are not touched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def at_0041():
    engine, cfg = migration_database("0041_encrypt_credentials")
    with engine.begin() as conn:
        for user_id, active in (("__system__", False), ("alice", True)):
            conn.execute(sa.text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :id, 'x', 'user', :active, :now)"), {"id": user_id, "active": active, "now": NOW})
    return engine, cfg


def _credential(conn, row_id, user_id, provider, credential_type, *, age_days=0, external_id=None):
    conn.execute(sa.text(
        "INSERT INTO provider_credentials (id, user_id, provider, credential_type, data, external_id, updated_at) "
        "VALUES (:id, :user, :provider, :type, :data, :external, :at)"),
        {"id": row_id, "user": user_id, "provider": provider, "type": credential_type, "data": f"sealed-{row_id}",
         "external": external_id, "at": NOW - timedelta(days=age_days)})


def _rows(engine) -> dict[str, tuple]:
    with engine.connect() as conn:
        return {
            row[0]: tuple(row[1:]) for row in conn.execute(sa.text(
                "SELECT id, user_id, provider, credential_type, external_id FROM provider_credentials"))
        }


def test_an_account_of_the_synthetic_user_becomes_the_shared_one(at_0041):
    engine, cfg = at_0041
    with engine.begin() as conn:
        _credential(conn, "own", "__system__", "sync", "credentials", external_id="ext-1")
        _credential(conn, "token", "__system__", "orbit", "oauth2_token")

    command.upgrade(cfg, "0042_open_mode_accounts")

    assert _rows(engine) == {
        "own": (None, "sync", "credentials", None),
        "token": (None, "orbit", "oauth2_token", None),
    }


def test_the_most_recent_of_two_accounts_of_the_same_provider_wins(at_0041):
    engine, cfg = at_0041
    with engine.begin() as conn:
        _credential(conn, "old-shared", None, "sync", "credentials", age_days=5)
        _credential(conn, "new-own", "__system__", "sync", "credentials", age_days=1)
        _credential(conn, "new-shared", None, "orbit", "oauth2_token", age_days=1)
        _credential(conn, "old-own", "__system__", "orbit", "oauth2_token", age_days=5)

    command.upgrade(cfg, "0042_open_mode_accounts")

    assert _rows(engine) == {
        "new-own": (None, "sync", "credentials", None),
        "new-shared": (None, "orbit", "oauth2_token", None),
    }


def test_nothing_else_is_touched(at_0041):
    engine, cfg = at_0041
    with engine.begin() as conn:
        _credential(conn, "alice", "alice", "sync", "credentials", external_id="ext-2")
        _credential(conn, "shared", None, "sync", "credentials")
        _credential(conn, "client", None, "orbit", "oauth2_client")
        _credential(conn, "key", None, "quay", "api_key")
    before = _rows(engine)

    command.upgrade(cfg, "0042_open_mode_accounts")

    assert _rows(engine) == before


def test_the_upgrade_can_run_on_an_instance_that_never_had_such_a_row_and_back(at_0041):
    engine, cfg = at_0041

    command.upgrade(cfg, "0042_open_mode_accounts")
    command.downgrade(cfg, "0041_encrypt_credentials")
    command.upgrade(cfg, "head")

    assert _rows(engine) == {}
