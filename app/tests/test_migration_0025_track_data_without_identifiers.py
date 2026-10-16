"""
Migration 0025: ``tracks.track_data`` loses its copy of the identifiers (they
live in ``track_identifiers``).
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


def _payload(engine, internal_id):
    with engine.connect() as conn:
        raw = conn.execute(sa.text("SELECT track_data FROM tracks WHERE internal_id = :i"), {"i": internal_id}).scalar()
    return raw


# ---------------------------------------------------------------------------
# 0025
# ---------------------------------------------------------------------------


def test_0025_removes_the_copy_of_the_identifiers_and_keeps_the_rest(db):
    engine, cfg = db
    with engine.begin() as conn:
        _track(conn, "a", "Song", "Band", identifiers=[("isrc", "I1"), ("fake_sync_id", "NEW")], data={
            "title": "Song", "year": 2001, "cover_url": "hostref:/cover/provider/p/1",
            "external_ids": [{"type": "fake_sync_id", "value": "OLD"}, {"type": "isrc", "value": "I1"}]})
        _track(conn, "none", "No data", "Band")
        _track(conn, "clean", "Clean", "Band", data={"title": "Clean"})
        _track(conn, "broken", "Broken", "Band", raw="{ not json")
        _track(conn, "list", "List", "Band", raw="[1, 2]")

    command.upgrade(cfg, "0025_track_data_without_identifiers")

    assert json.loads(_payload(engine, "a")) == {"title": "Song", "year": 2001, "cover_url": "hostref:/cover/provider/p/1"}
    assert _payload(engine, "none") is None
    assert json.loads(_payload(engine, "clean")) == {"title": "Clean"}
    assert _payload(engine, "broken") == "{ not json"
    assert _payload(engine, "list") == "[1, 2]"
    with engine.connect() as conn:
        # The table is untouched: it is the authority.
        assert sorted(conn.execute(sa.text("SELECT id_type, value FROM track_identifiers")).fetchall()) == [
            ("fake_sync_id", "NEW"), ("isrc", "I1")]


def test_0025_downgrade_rebuilds_the_copy_from_the_table(db):
    engine, cfg = db
    with engine.begin() as conn:
        _track(conn, "a", "Song", "Band", identifiers=[("isrc", "I1"), ("catalog_id", "S1"), ("fake_sync_id", "NEW")],
               data={"title": "Song", "external_ids": [{"type": "fake_sync_id", "value": "OLD"}]})
    command.upgrade(cfg, "0025_track_data_without_identifiers")

    command.downgrade(cfg, "0024_refresh_token_rotation")

    assert json.loads(_payload(engine, "a"))["external_ids"] == [
        {"type": "catalog_id", "value": "S1", "provider": "catalog"},
        {"type": "fake_sync_id", "value": "NEW"},
        {"type": "isrc", "value": "I1"},
    ]
    command.upgrade(cfg, "0025_track_data_without_identifiers")
    assert "external_ids" not in json.loads(_payload(engine, "a"))
