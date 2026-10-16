"""
Migration 0037: the id of a linked account (unique per provider among the
rows that carry one), the pending flag of users and the table of one-time
login codes. What existed keeps working and carries nothing new.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.database import migration_database

NOW = datetime(2026, 10, 10, 12, 0)


@pytest.fixture
def db():
    return migration_database("0036_linked_accounts")


def _user(conn, user_id, active=True):
    conn.execute(sa.text(
        "INSERT INTO users (id, username, password_hash, role, is_active, created_at) VALUES (:i, :i, 'x', 'user', :a, :n)"),
        {"i": user_id, "a": active, "n": NOW})


def _credential(conn, row_id, user_id, provider, **extra):
    columns = ["id", "user_id", "provider", "credential_type", "data", "updated_at", *extra]
    conn.execute(
        sa.text(f"INSERT INTO provider_credentials ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})"),
        {"id": row_id, "user_id": user_id, "provider": provider, "credential_type": "oauth2_token",
         "data": json.dumps({"access_token": "t"}), "updated_at": NOW, **extra},
    )


def test_0037_keeps_what_exists_and_adds_nothing_to_it(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
        _user(conn, "off", active=False)
        _credential(conn, "own", "alice", "origin")

    command.upgrade(cfg, "0037_provider_login")

    with engine.connect() as conn:
        users = {row.id: row for row in conn.execute(sa.text("SELECT id, is_active, pending_approval FROM users"))}
        assert [(u.is_active, u.pending_approval) for u in (users["alice"], users["off"])] == [(True, False), (False, False)]
        assert conn.execute(sa.text("SELECT external_id FROM provider_credentials WHERE id = 'own'")).scalar() is None
        assert conn.execute(sa.text("SELECT count(*) FROM provider_login_codes")).scalar() == 0


def test_0037_makes_the_id_unique_per_provider_among_the_rows_that_have_one(db):
    engine, cfg = db
    with engine.begin() as conn:
        for user_id in ("alice", "bob"):
            _user(conn, user_id)
    command.upgrade(cfg, "0037_provider_login")

    with engine.begin() as conn:
        _credential(conn, "a1", "alice", "origin", external_id="ext-1")
        _credential(conn, "a2", "alice", "other", external_id="ext-1")  # another provider: another account
        _credential(conn, "b-none", "bob", "origin")                     # no id
        _credential(conn, "b-none-2", "bob", "other")                    # many rows without one are fine
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _credential(conn, "b1", "bob", "origin", external_id="ext-1")


def test_0037_downgrades_to_what_was_before(db):
    engine, cfg = db
    command.upgrade(cfg, "0037_provider_login")

    command.downgrade(cfg, "0036_linked_accounts")

    inspector = sa.inspect(engine)
    assert "provider_login_codes" not in inspector.get_table_names()
    assert "external_id" not in {c["name"] for c in inspector.get_columns("provider_credentials")}
    assert "pending_approval" not in {c["name"] for c in inspector.get_columns("users")}
    assert "uq_provider_credential_external" not in {i["name"] for i in inspector.get_indexes("provider_credentials")}
    assert "uq_provider_credential_global" in {i["name"] for i in inspector.get_indexes("provider_credentials")}
