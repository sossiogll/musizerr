"""
URL serviti dall'host letti dal DB. ``tracks.track_data`` e
``user_saved_track_providers.stream_url`` non contengono mai un URL pubblico:
un URL dell'host si salva come riferimento senza prefisso (``hostref:``) e
si ricostruisce col prefisso corrente a ogni lettura. Anche le righe scritte
prima (``/stream/provider/...`` senza prefisso, o con un prefisso vecchio)
escono col prefisso corrente; gli URL assoluti dei provider restano intatti.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core import urls
from core.serialization import serialize_track
from db.engine import get_session
from db.models import (
    SYSTEM_USER_ID,
    Download,
    TrackRecord,
    User,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.track_store import find_or_create_track, track_from_record
from routes import deps
from routes.history import router as history_router
from routes.library import router as library_router
from services import public_urls
from services.config_manager import ConfigManager

from tests.database import migration_database
from tests.conftest import FakeAuthManager, FakeRegistry, make_track

CDN_COVER = "https://cdn.example.com/covers/abc.jpg"
CDN_PREVIEW = "https://cdn.example.com/previews/abc.mp3"


@pytest.fixture(params=["/api", "/music/api"])
def prefix(request, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_PREFIX", request.param)
    return request.param


# ---------------------------------------------------------------------------
# core.urls: forma persistita e forma pubblica
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [
    "/cover/provider/prov/al-9",
    "/api/cover/provider/prov/al-9",
    "/old/prefix/cover/provider/prov/al-9",
    "hostref:/cover/provider/prov/al-9",
])
def test_a_host_cover_url_under_any_prefix_is_stored_as_a_reference(stored):
    assert urls.to_host_ref(stored) == "hostref:/cover/provider/prov/al-9"


@pytest.mark.parametrize("stored, route", [
    ("/stream/provider/prov/song-1", "/stream/provider/prov/song-1"),
    ("/api/stream/provider/prov/song-1", "/stream/provider/prov/song-1"),
    ("/stream/track-uuid", "/stream/track-uuid"),
    ("/api/stream/track-uuid", "/stream/track-uuid"),
])
def test_stream_urls_are_stored_as_references(stored, route):
    assert urls.to_host_ref(stored) == "hostref:" + route


def test_the_reference_form_is_idempotent():
    once = urls.to_host_ref("/api/cover/provider/prov/a%2Fb")
    assert urls.to_host_ref(once) == once
    assert urls.publish("/api", once) == "/api/cover/provider/prov/a%2Fb"


@pytest.mark.parametrize("value", [CDN_COVER, CDN_PREVIEW, "//cdn.example.com/x.jpg", None, ""])
def test_absolute_provider_urls_are_left_untouched(value):
    assert urls.to_host_ref(value) == value
    assert urls.publish("/api", value) == value


@pytest.mark.parametrize("value", ["/", "/favicon.ico", "/api/", "/cover/provider//x", "hostref:/nope"])
def test_an_unknown_root_relative_path_is_neither_stored_nor_served(value):
    assert urls.to_host_ref(value) is None
    assert urls.publish("/api", value) is None


def test_publishing_a_stored_track_uses_the_configured_prefix(prefix):
    stored = {
        "cover_url": "/cover/provider/prov/al-9",
        "stream_url": "hostref:/stream/provider/prov/song-1",
        "preview_url": CDN_PREVIEW,
    }

    published = public_urls.publish_track_dict(stored)

    assert published["cover_url"] == f"{prefix}/cover/provider/prov/al-9"
    assert published["stream_url"] == f"{prefix}/stream/provider/prov/song-1"
    assert published["preview_url"] == CDN_PREVIEW


# ---------------------------------------------------------------------------
# Scrittura: find_or_create_track non persiste mai un URL pubblico
# ---------------------------------------------------------------------------


def test_new_and_merged_tracks_are_stored_with_references(engine, prefix):
    track = make_track("Song", "Artist", provider="prov", provider_id="t1", isrc="ISRC1")
    track.cover_url = public_urls.cover_url("prov", "al-9")
    track.stream_url = public_urls.provider_stream_url("prov", "song-1")

    with Session(engine) as session:
        record = find_or_create_track(track, session)
        data = json.loads(record.track_data)
        assert data["cover_url"] == "hostref:/cover/provider/prov/al-9"
        assert data["stream_url"] == "hostref:/stream/provider/prov/song-1"

        other = make_track("Song", "Artist", provider="cdn", provider_id="c1", isrc="ISRC1")
        other.preview_url = CDN_PREVIEW
        merged = find_or_create_track(other, session)
        data = json.loads(merged.track_data)
        assert merged.internal_id == record.internal_id
        assert data["cover_url"] == "hostref:/cover/provider/prov/al-9"
        assert data["preview_url"] == CDN_PREVIEW
        assert track_from_record(merged).cover_url == "hostref:/cover/provider/prov/al-9"


# ---------------------------------------------------------------------------
# Lettura: /favorites e /downloads con righe vecchie
# ---------------------------------------------------------------------------


def _old_track_data(provider_id: str, *, cover: str | None, stream: str | None, preview: str | None = None) -> str:
    track = make_track(f"Song {provider_id}", "Artist", provider="prov", provider_id=provider_id)
    data = serialize_track(track)
    data.update({"cover_url": cover, "stream_url": stream, "preview_url": preview})
    return json.dumps(data)


def _seed_old_rows(engine) -> None:
    """Righe scritte prima dei riferimenti: URL dell'host senza prefisso, col
    prefisso di allora, e URL assoluti di un CDN."""
    with Session(engine) as session:
        session.add(User(id=SYSTEM_USER_ID, username="system", password_hash="", role="admin"))
        rows = [
            TrackRecord(title="Song a", artist="Artist", album="Album", track_data=_old_track_data(
                "a", cover="/cover/provider/prov/al-a", stream="/stream/provider/prov/song-a")),
            TrackRecord(title="Song b", artist="Artist", album="Album", track_data=_old_track_data(
                "b", cover="/api/cover/provider/prov/al-b", stream="/api/stream/provider/prov/song-b")),
            TrackRecord(title="Song c", artist="Artist", album="Album", track_data=_old_track_data(
                "c", cover=CDN_COVER, stream=None, preview=CDN_PREVIEW)),
            TrackRecord(title="Song d", artist="Artist", album="Album", track_data=_old_track_data(
                "d", cover=None, stream=None)),
        ]
        session.add_all(rows)
        session.commit()
        for record in rows:
            saved = UserSavedTrack(user_id=SYSTEM_USER_ID, track_id=record.internal_id)
            session.add(saved)
            session.add(UserFavorite(user_id=SYSTEM_USER_ID, track_id=record.internal_id))
            session.flush()
            session.add(Download(track_id=record.internal_id, requested_by=SYSTEM_USER_ID, status="downloaded",
                                 started_at=datetime.now(timezone.utc)))
            if record.title == "Song d":
                # Solo lo stream del provider di libreria, salvato senza prefisso.
                session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider="prov",
                                                   stream_url="/stream/provider/prov/song-d"))
        session.commit()


@pytest.fixture
def client(engine):
    _seed_old_rows(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    app = FastAPI()
    app.include_router(library_router)
    app.include_router(history_router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry()
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()
    with TestClient(app) as c:
        yield c


def _tracks(path: str, body: dict) -> dict[str, dict]:
    items = body["items"]
    tracks = [item["track"] for item in items] if path == "/downloads" else items
    return {t["provider_id"]: t for t in tracks}


@pytest.mark.parametrize("path", ["/favorites", "/downloads"])
def test_old_rows_come_back_with_the_prefix(client, prefix, path):
    resp = client.get(path)
    assert resp.status_code == 200
    tracks = _tracks(path, resp.json())

    assert tracks["a"]["cover_url"] == f"{prefix}/cover/provider/prov/al-a"
    assert tracks["b"]["cover_url"] == f"{prefix}/cover/provider/prov/al-b"
    assert tracks["c"]["cover_url"] == CDN_COVER
    assert tracks["c"]["preview_url"] == CDN_PREVIEW
    # In libreria: si ascolta il nostro file, dall'URL di streaming dell'host.
    for track in tracks.values():
        assert track["stream_url"] == f"{prefix}/stream/{track['library_id']}"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("path", ["/favorites", "/downloads"])
def test_no_url_in_the_response_lacks_the_prefix(client, prefix, path):
    body = client.get(path).json()

    tracks = list(_tracks(path, body).values())
    url_values = [
        v for t in tracks
        for key in ("cover_url", "stream_url", "preview_url") if (v := t.get(key))
    ] + [
        info["stream_url"] for t in tracks
        for info in (t.get("provider_saved_at") or {}).values() if info.get("stream_url")
    ]
    rooted = [u for u in url_values if u.startswith("/")]
    assert rooted, "le righe seminate servono almeno un URL dell'host"
    assert [u for u in rooted if not u.startswith(prefix + "/")] == []
    assert not [s for s in _strings(body) if s.startswith(urls.HOST_REF_SCHEME)]


# ---------------------------------------------------------------------------
# Migrazione 0019 su un DB temporaneo
# ---------------------------------------------------------------------------

_APP_ROOT = Path(__file__).resolve().parent.parent



@pytest.fixture
def engine_at_0018():
    return migration_database("0018_drop_duplicate_members_group_index")


def _insert_track(conn, internal_id: str, data) -> None:
    conn.execute(
        sa.text("INSERT INTO tracks (internal_id, title, artist, album, duration_ms, created_at, track_data) "
                "VALUES (:id, 'T', 'A', 'B', 0, :now, :data)"),
        {"id": internal_id, "now": datetime.now(timezone.utc), "data": data if isinstance(data, str) or data is None else json.dumps(data)},
    )


def _track_data(conn, internal_id: str):
    raw = conn.execute(sa.text("SELECT track_data FROM tracks WHERE internal_id = :id"), {"id": internal_id}).scalar()
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _seed_for_migration(conn) -> str:
    _insert_track(conn, "old", {"title": "T", "cover_url": "/cover/provider/prov/al-1",
                                "stream_url": "/stream/provider/prov/s-1", "preview_url": CDN_PREVIEW})
    _insert_track(conn, "prefixed", {"title": "T", "cover_url": "/api/cover/provider/prov/al-2",
                                     "stream_url": "/api/stream/provider/prov/s-2"})
    _insert_track(conn, "cdn", {"title": "T", "cover_url": CDN_COVER, "preview_url": CDN_PREVIEW})
    _insert_track(conn, "junk", {"title": "T", "cover_url": "/favicon.ico"})
    _insert_track(conn, "broken", "{ not json")
    _insert_track(conn, "empty", None)
    conn.execute(sa.text(
        "INSERT INTO users (id, username, password_hash, role, created_at, is_active) "
        "VALUES ('u1', 'u', '', 'user', :now, true)"), {"now": datetime.now(timezone.utc)})
    conn.execute(sa.text(
        "INSERT INTO user_saved_tracks (id, user_id, track_id, saved_at) VALUES ('st1', 'u1', 'old', :now)"),
        {"now": datetime.now(timezone.utc)})
    provider_row = str(uuid.uuid4())
    conn.execute(sa.text(
        "INSERT INTO user_saved_track_providers (id, saved_track_id, provider, provider_track_id, synced_at, stream_url) "
        "VALUES (:id, 'st1', 'prov', 's-1', :now, '/stream/provider/prov/s-1')"),
        {"id": provider_row, "now": datetime.now(timezone.utc)})
    return provider_row


def _assert_migrated(conn, provider_row: str) -> None:
    assert _track_data(conn, "old") == {
        "title": "T", "cover_url": "hostref:/cover/provider/prov/al-1",
        "stream_url": "hostref:/stream/provider/prov/s-1", "preview_url": CDN_PREVIEW,
    }
    assert _track_data(conn, "prefixed")["cover_url"] == "hostref:/cover/provider/prov/al-2"
    assert _track_data(conn, "prefixed")["stream_url"] == "hostref:/stream/provider/prov/s-2"
    assert _track_data(conn, "cdn") == {"title": "T", "cover_url": CDN_COVER, "preview_url": CDN_PREVIEW}
    assert _track_data(conn, "junk") == {"title": "T", "cover_url": None}
    assert _track_data(conn, "broken") == "{ not json"
    assert _track_data(conn, "empty") is None
    assert conn.execute(
        sa.text("SELECT stream_url FROM user_saved_track_providers WHERE id = :id"), {"id": provider_row}
    ).scalar() == "hostref:/stream/provider/prov/s-1"
    rooted = conn.execute(sa.text(
        "SELECT COUNT(*) FROM tracks WHERE track_data LIKE '%\"/stream/%' OR track_data LIKE '%\"/cover/%' "
        "OR track_data LIKE '%\"/api/%'"
    )).scalar()
    assert rooted == 0


def test_migration_rewrites_stored_host_urls_as_references(engine_at_0018):
    engine, cfg = engine_at_0018
    with engine.begin() as conn:
        provider_row = _seed_for_migration(conn)

    command.upgrade(cfg, "0019_host_url_refs")

    with engine.connect() as conn:
        _assert_migrated(conn, provider_row)


def test_migration_is_idempotent(engine_at_0018):
    engine, cfg = engine_at_0018
    with engine.begin() as conn:
        provider_row = _seed_for_migration(conn)

    command.upgrade(cfg, "0019_host_url_refs")
    with engine.connect() as conn:
        first = {row[0]: row[1] for row in conn.execute(sa.text("SELECT internal_id, track_data FROM tracks"))}

    # Rieseguire la stessa conversione su righe già migrate non cambia nulla.
    command.downgrade(cfg, "0018_drop_duplicate_members_group_index")
    command.upgrade(cfg, "0019_host_url_refs")
    with engine.connect() as conn:
        second = {row[0]: row[1] for row in conn.execute(sa.text("SELECT internal_id, track_data FROM tracks"))}
        _assert_migrated(conn, provider_row)
    assert first == second

    spec = importlib.util.spec_from_file_location(
        "migration_0019", _APP_ROOT / "migrations" / "versions" / "0019_host_url_refs.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        migration._rewrite_track_data(conn, urls.to_host_ref)
        migration._rewrite_saved_stream_urls(conn, urls.to_host_ref)
    with engine.connect() as conn:
        third = {row[0]: row[1] for row in conn.execute(sa.text("SELECT internal_id, track_data FROM tracks"))}
    assert third == second


def test_downgrade_restores_public_urls_with_the_current_prefix(engine_at_0018, monkeypatch):
    engine, cfg = engine_at_0018
    monkeypatch.setenv("PUBLIC_API_PREFIX", "/api")
    with engine.begin() as conn:
        _seed_for_migration(conn)

    command.upgrade(cfg, "0019_host_url_refs")
    command.downgrade(cfg, "0018_drop_duplicate_members_group_index")

    with engine.connect() as conn:
        assert _track_data(conn, "old")["cover_url"] == "/api/cover/provider/prov/al-1"
        assert _track_data(conn, "cdn")["cover_url"] == CDN_COVER
