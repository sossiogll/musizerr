"""
Migration 0036: a disconnected OAuth2 account is a deleted row, not a
``{"revoked": true}`` marker. Every live account keeps working untouched,
the administrator's own and the shared copy of it included.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 10, 10, 12, 0)


@pytest.fixture
def db():
    return migration_database("0035_download_requests")


def _user(conn, user_id):
    conn.execute(sa.text(
        "INSERT INTO users (id, username, password_hash, role, is_active, created_at) VALUES (:i, :i, 'x', 'user', :t, :n)"),
        {"i": user_id, "t": True, "n": NOW})


def _credential(conn, row_id, user_id, provider, credential_type, data):
    conn.execute(sa.text(
        "INSERT INTO provider_credentials (id, user_id, provider, credential_type, data, updated_at) "
        "VALUES (:id, :u, :p, :t, :d, :n)"),
        {"id": row_id, "u": user_id, "p": provider, "t": credential_type, "d": json.dumps(data), "n": NOW})


def _ids(engine):
    with engine.connect() as conn:
        return sorted(row[0] for row in conn.execute(sa.text("SELECT id FROM provider_credentials")))


def test_0036_deletes_the_revoked_markers_and_nothing_else(db):
    engine, cfg = db
    with engine.begin() as conn:
        for user_id in ("alice", "admin"):
            _user(conn, user_id)
        _credential(conn, "alice-marker", "alice", "origin", "oauth2_token", {"revoked": True})
        _credential(conn, "shared-marker", None, "other", "oauth2_token", {"revoked": True})
        _credential(conn, "alice-live", "alice", "second", "oauth2_token", {"access_token": "a", "refresh_token": "r"})
        # The administrator's own account and the shared copy the old code made of it: both stay.
        _credential(conn, "admin-own", "admin", "origin", "oauth2_token", {"access_token": "t", "refresh_token": "r"})
        _credential(conn, "admin-shared", None, "origin", "oauth2_token", {"access_token": "t", "refresh_token": "r"})
        _credential(conn, "client", None, "origin", "oauth2_client", {"client_id": "c", "client_secret": "s"})
        _credential(conn, "login", "alice", "origin", "credentials", {"username": "u", "password": "p"})
        _credential(conn, "not-a-marker", "alice", "third", "oauth2_token", {"revoked": False, "access_token": "x"})
        _credential(conn, "key", None, "origin", "api_key", {"api_key": "k"})

    command.upgrade(cfg, "0036_linked_accounts")

    assert _ids(engine) == sorted([
        "alice-live", "admin-own", "admin-shared", "client", "login", "not-a-marker", "key",
    ])


def test_0036_leaves_an_unreadable_row_alone(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
        conn.execute(sa.text(
            "INSERT INTO provider_credentials (id, user_id, provider, credential_type, data, updated_at) "
            "VALUES ('broken', 'alice', 'origin', 'oauth2_token', '{ not json', :n)"), {"n": NOW})

    command.upgrade(cfg, "0036_linked_accounts")

    assert _ids(engine) == ["broken"]
