"""
Migration 0026: ``removed_favorites``, filled from the matched likes that have
no favorite (the removals made before).
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command

from tests.database import migration_database

NOW = datetime(2026, 10, 3, 12, 0)



@pytest.fixture
def db():
    engine, cfg = migration_database("0024_refresh_token_rotation")
    return engine, cfg


def _track(conn, internal_id, title, artist, *, data=None, raw=None, identifiers=(), duration_ms=200_000):
    payload = raw if raw is not None else (json.dumps(data) if data is not None else None)
    conn.execute(sa.text(
        "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at, track_data) "
        "VALUES (:id, :title, :artist, 'Album', :d, :now, :data)"),
        {"id": internal_id, "title": title, "artist": artist, "d": duration_ms, "now": NOW, "data": payload})
    for index, (id_type, value) in enumerate(identifiers):
        conn.execute(sa.text("INSERT INTO track_identifiers (id, track_id, id_type, value) VALUES (:i, :t, :y, :v)"),
                     {"i": f"{internal_id}-{index}", "t": internal_id, "y": id_type, "v": value})


# ---------------------------------------------------------------------------
# 0026
# ---------------------------------------------------------------------------


def _like(conn, like_id, user_id, track_id, library_id=None, matched=False):
    conn.execute(sa.text(
        "INSERT INTO user_saved_tracks (id, user_id, track_id, saved_at, library_track_id, matched_at) "
        "VALUES (:id, :u, :t, :now, :l, :m)"),
        {"id": like_id, "u": user_id, "t": track_id, "now": NOW, "l": library_id, "m": NOW if matched else None})


def test_0026_remembers_the_removals_made_before(db):
    engine, cfg = db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
            "VALUES ('u', 'alice', 'x', 'user', true, :now)"), {"now": NOW})
        # Favorite taken off: matched like, no favorite.
        _track(conn, "gone", "Killing In The Name", "Rage Against The Machine", identifiers=[("isrc", "USSM19200022")],
               data={"title": "Killing In The Name", "artist": "Rage Against The Machine"})
        _like(conn, "l1", "u", "gone", "gone", matched=True)
        # Favorite kept.
        _track(conn, "kept", "Bulls on Parade", "Rage Against The Machine", identifiers=[("isrc", "USSM19600001")])
        _like(conn, "l2", "u", "kept", "kept", matched=True)
        conn.execute(sa.text("INSERT INTO user_favorites (id, user_id, track_id, source, created_at) "
                             "VALUES ('f', 'u', 'kept', 'match', :now)"), {"now": NOW})
        # Not matched yet: nothing was ever favorited, nothing to remember.
        _track(conn, "elsewhere", "Wake Up", "Rage Against The Machine")
        _like(conn, "l3", "u", "elsewhere")

    command.upgrade(cfg, "0026_removed_favorites")

    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT user_id, key, group_id FROM removed_favorites ORDER BY key")).fetchall()
    assert [(user, key) for user, key, _ in rows] == [
        ("u", "isrc:USSM19200022"),
        ("u", "song:killing in the name|rage against the machine"),
    ]
    assert len({group for *_, group in rows}) == 1


def test_0026_is_idempotent_and_downgrades(db):
    engine, cfg = db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
            "VALUES ('u', 'alice', 'x', 'user', true, :now)"), {"now": NOW})
        _track(conn, "gone", "Song", "Band", identifiers=[("isrc", "I1")])
        _like(conn, "l1", "u", "gone", "gone", matched=True)
    command.upgrade(cfg, "0026_removed_favorites")
    command.downgrade(cfg, "0025_track_data_without_identifiers")
    assert "removed_favorites" not in sa.inspect(engine).get_table_names()

    command.upgrade(cfg, "0026_removed_favorites")

    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM removed_favorites")).scalar() == 2
