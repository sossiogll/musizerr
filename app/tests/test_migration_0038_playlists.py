"""
Migration 0038: the tables of the playlists. A playlist is unique per user and
source (an own playlist has none), a track is in a playlist once, and the copy
of a playlist on a sync provider is keyed by playlist and provider without a
foreign key to the playlist (it outlives it). What existed is untouched.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.database import migration_database

NOW = datetime(2026, 10, 10, 12, 0)


@pytest.fixture
def db():
    return migration_database("0037_provider_login")


def _user(conn, user_id):
    conn.execute(sa.text(
        "INSERT INTO users (id, username, password_hash, role, is_active, created_at, pending_approval) "
        "VALUES (:i, :i, 'x', 'user', :a, :n, :p)"), {"i": user_id, "a": True, "n": NOW, "p": False})


def _track(conn, track_id):
    conn.execute(sa.text(
        "INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at) "
        "VALUES (:i, 't', 'a', 'b', 0, :n)"), {"i": track_id, "n": NOW})


def _playlist(conn, playlist_id, user_id, **extra):
    values = {"id": playlist_id, "user_id": user_id, "name": "P", "created_at": NOW, "updated_at": NOW, **extra}
    conn.execute(sa.text(
        f"INSERT INTO playlists ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})"), values)


def test_0038_adds_the_tables_and_touches_nothing_else(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
        _track(conn, "t1")

    command.upgrade(cfg, "0038_playlists")

    inspector = sa.inspect(engine)
    assert {"playlists", "playlist_tracks", "playlist_syncs"} <= set(inspector.get_table_names())
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM tracks")).scalar() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM playlists")).scalar() == 0


def test_0038_defaults_describe_an_own_playlist(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
    command.upgrade(cfg, "0038_playlists")

    with engine.begin() as conn:
        _playlist(conn, "p1", "alice")
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT kind, description, auto_download, source_provider FROM playlists WHERE id = 'p1'")).one()
    assert (row.kind, row.description, bool(row.auto_download), row.source_provider) == ("own", "", False, None)


def test_0038_a_source_is_followed_once_per_user_and_own_playlists_are_unlimited(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
        _user(conn, "bob")
    command.upgrade(cfg, "0038_playlists")

    with engine.begin() as conn:
        _playlist(conn, "a1", "alice", source_provider="origin", source_id="s1")
        _playlist(conn, "b1", "bob", source_provider="origin", source_id="s1")  # another user: fine
        _playlist(conn, "own1", "alice")
        _playlist(conn, "own2", "alice")  # no source: as many as the user wants
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _playlist(conn, "a2", "alice", source_provider="origin", source_id="s1")


def test_0038_a_track_is_in_a_playlist_once(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
        _track(conn, "t1")
    command.upgrade(cfg, "0038_playlists")
    with engine.begin() as conn:
        _playlist(conn, "p1", "alice")
        conn.execute(sa.text(
            "INSERT INTO playlist_tracks (id, playlist_id, track_id, position, added_at) VALUES ('r1', 'p1', 't1', 0, :n)"),
            {"n": NOW})

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO playlist_tracks (id, playlist_id, track_id, position, added_at) "
                "VALUES ('r2', 'p1', 't1', 1, :n)"), {"n": NOW})


def test_0038_a_copy_on_a_provider_outlives_its_playlist(db):
    engine, cfg = db
    with engine.begin() as conn:
        _user(conn, "alice")
    command.upgrade(cfg, "0038_playlists")

    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO playlist_syncs (playlist_id, sync_provider, user_id, remote_id, content_hash, synced_at) "
            "VALUES ('deleted-playlist', 'server', 'alice', 'r1', '', :n)"), {"n": NOW})
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM playlist_syncs")).scalar() == 1


def test_0038_downgrades_to_what_was_before(db):
    engine, cfg = db
    command.upgrade(cfg, "0038_playlists")

    command.downgrade(cfg, "0037_provider_login")

    assert not {"playlists", "playlist_tracks", "playlist_syncs"} & set(sa.inspect(engine).get_table_names())
