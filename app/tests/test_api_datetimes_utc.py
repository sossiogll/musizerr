"""
Contract: every datetime the API returns carries its offset (``Z`` or
``+00:00``). SQLite hands back naive datetimes; serialized as they were
("2026-09-29T09:38:06"), the browser read them as LOCAL time and the routines
page said a run finished "3 hours ago" when it was one (Europe/Rome, UTC+2).

The sweep calls every GET route without path parameters as an admin, over a
database with a row in every table that has a datetime shown by the API, and
checks every string that looks like a datetime in every answer.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime
from sqlmodel import Session, SQLModel, select

import main
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import (
    Download,
    DuplicateGroup,
    DuplicateMember,
    InstalledProvider,
    LibraryJobRun,
    ProviderSource,
    RoutineRun,
    TrackIdentifierRecord,
    TrackRecord,
    User,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.types import UTCDateTime
from routes import deps
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, FakeSyncProvider
from tests.route_support import api_routes

# An ISO datetime (date, "T", time); its zone is checked separately.
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?")
WITH_OFFSET = re.compile(r"(Z|[+-]\d{2}:\d{2})$")

# Streams, documents and compatibility shims: not JSON answers to sweep.
SKIPPED = {"/progress", "/health", "/openapi.yaml", "/search"}

# The routes that return the datetimes seeded below: the sweep must reach them.
MUST_REACH = {
    "/system/routines", "/downloads", "/favorites", "/favorites/elsewhere",
    "/library/duplicates", "/library/jobs", "/sources/providers/installed",
}

NOW = datetime.now(timezone.utc) - timedelta(minutes=5)


def _seed(session: Session) -> User:
    admin = User(username="root", password_hash="x", role="admin")
    session.add(admin)
    track = TrackRecord(title="Song", artist="A", album="B", duration_ms=1000)
    session.add(track)
    session.flush()
    session.add(TrackIdentifierRecord(track_id=track.internal_id, id_type="provider_id", value="1", provider="fake"))
    session.add(Download(track_id=track.internal_id, status="downloaded", requested_by=admin.id,
                         started_at=NOW, completed_at=NOW, file_path="A/B/Song.flac"))
    session.add(UserFavorite(user_id=admin.id, track_id=track.internal_id, created_at=NOW))
    saved = UserSavedTrack(user_id=admin.id, track_id=track.internal_id, saved_at=NOW)
    session.add(saved)
    session.flush()
    session.add(UserSavedTrackProvider(saved_track_id=saved.id, provider="fake", provider_track_id="1",
                                       added_at=NOW, synced_at=NOW))
    session.add(RoutineRun(routine_key="match_likes", started_at=NOW, finished_at=NOW, status="ok", detail="{}"))
    session.add(LibraryJobRun(user_id=admin.id, kind="library_sync", status="completed",
                              created_at=NOW, started_at=NOW, heartbeat_at=NOW, completed_at=NOW))
    group = DuplicateGroup(recording_id="r", status="resolved", created_at=NOW, updated_at=NOW, resolved_at=NOW)
    session.add(group)
    session.flush()
    session.add(DuplicateMember(group_id=group.id, sync_id="s", path="a.flac", title="Song", artist="A",
                                deleted_at=NOW))
    # Disabled: the installed list would otherwise look for an update at a URL that goes nowhere.
    source = ProviderSource(name="Registry", url="https://registry.test/catalog.json", added_at=NOW, enabled=False)
    session.add(source)
    session.flush()
    session.add(InstalledProvider(
        source_id=source.id, name="fake", image="registry.test/musizerr-provider-fake", digest="sha256:" + "a" * 64,
        provider_api_version=5, label="Fake", roles='["metadata"]', permissions="{}", approved_at=NOW, installed_at=NOW,
    ))
    session.commit()
    session.refresh(admin)
    return admin


@pytest.fixture
def admin_client():
    engine = new_engine()
    with Session(engine) as session:
        admin = _seed(session)
        token = create_access_token(admin.id, admin.role)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    registry = FakeRegistry(metadata=[FakeMetadataProvider("fake")], syncs=[FakeSyncProvider("fakesync")])

    def _session():
        with Session(engine) as s:
            yield s

    overrides = {
        get_session: _session,
        deps.get_config: lambda: config,
        deps.get_registry: lambda: registry,
        deps.get_auth_manager: lambda: FakeAuthManager(),
    }
    main.app.dependency_overrides.update(overrides)
    client = TestClient(main.app, raise_server_exceptions=False)
    client.headers["Authorization"] = f"Bearer {token}"
    client.engine = engine
    yield client
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)


def _strings(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _sweepable_routes() -> list[str]:
    return sorted({
        route.path for route in api_routes()
        if "GET" in route.methods and "{" not in route.path
        and route.path not in SKIPPED and not route.path.startswith(("/api/", "/credentials"))
    })


def test_every_datetime_in_every_answer_carries_its_offset(admin_client):
    naive: list[str] = []
    reached: set[str] = set()
    for route in _sweepable_routes():
        resp = admin_client.get(route)
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            continue
        reached.add(route)
        for where, text in _strings(resp.json()):
            if ISO_DATETIME.match(text) and not WITH_OFFSET.search(text):
                naive.append(f"{route}{where} = {text}")

    assert MUST_REACH <= reached, f"not reached: {sorted(MUST_REACH - reached)}"
    assert naive == [], "datetimes without an offset:\n" + "\n".join(naive)


def test_every_datetime_column_reads_back_as_utc(admin_client):
    columns = [
        f"{table.name}.{column.name}"
        for table in SQLModel.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime) or isinstance(getattr(column.type, "impl", None), DateTime)
        if not isinstance(column.type, UTCDateTime)
    ]
    assert columns == [], "datetime columns without UTCDateTime: " + ", ".join(columns)

    with Session(admin_client.engine) as session:
        run = session.exec(select(RoutineRun)).one()
    assert run.started_at.tzinfo is not None
    assert run.started_at.utcoffset() == timedelta(0)
