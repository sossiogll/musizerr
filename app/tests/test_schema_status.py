"""
The schema the database is at, shown to admins (ADR-0007 §2): ``GET /health`` and ``GET /admin/updates`` (the
About page). Admins only, like the version; never an obstacle to a status page when the database cannot answer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.user_auth import create_access_token
from db.engine import head_revisions
from routes import deps
from routes.system import health
from routes.system.health import router as health_router
from services import update_check
from services.config_manager import ConfigManager
from services.database_init import init_db
from services.schema_status import schema_status

from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry
from tests.database import new_engine

[HEAD] = head_revisions()
NOW = datetime(2026, 10, 24, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def migrated(tmp_path):
    """A database migrated by `init` as version 1.5.0 (an empty one: no backup is taken)."""
    engine = new_engine(schema=False, addressable=True)
    init_db(engine, version="1.5.0", directory=tmp_path / "backups", now=lambda: NOW)
    return engine


def _client(monkeypatch, engine, *, auth_enabled: bool = True) -> TestClient:
    config = ConfigManager(engine=new_engine())
    config.update({"auth_enabled": auth_enabled})
    monkeypatch.setattr(health, "engine", engine)
    registry = FakeRegistry(metadata=[FakeMetadataProvider("m1")], downloads=[FakeDownloadProvider("d1")])
    monkeypatch.setattr(health, "get_registry", lambda: registry)
    app = FastAPI()
    app.include_router(health_router)
    app.dependency_overrides[deps.get_config] = lambda: config
    return TestClient(app)


def _admin() -> dict:
    return {"Authorization": f"Bearer {create_access_token('admin-id', 'admin')}"}


def test_the_status_says_where_the_database_is_and_what_migrated_it(migrated):
    status = schema_status(migrated)

    assert status["revision"] == HEAD
    assert status["migrated_by"] == "1.5.0"
    assert status["phase"] in ("expand", "contract")
    assert status["contract_revision"] is not None
    assert status["migrated_at"].endswith(("Z", "+00:00"))
    assert set(status) == {"revision", "migrated_by", "min_app_version", "contract_revision", "phase", "migrated_at"}


def test_a_database_migrated_by_hand_has_its_revision_and_no_record():
    from db.engine import upgrade_to_head

    engine = new_engine(schema=False, addressable=True)
    upgrade_to_head(engine)  # `alembic upgrade head`: the schema_info table exists, init never wrote its row

    assert schema_status(engine) == {
        "revision": HEAD, "migrated_by": None, "min_app_version": None, "contract_revision": None,
        "phase": None, "migrated_at": None,
    }


def test_a_database_that_was_never_migrated_has_no_status():
    assert schema_status(new_engine(schema=False)) is None


def test_a_database_that_does_not_answer_has_no_status_and_no_error():
    class Broken:
        def connect(self):
            raise RuntimeError("database is locked")

    assert schema_status(Broken()) is None


def test_health_tells_an_admin_the_schema(monkeypatch, migrated):
    data = _client(monkeypatch, migrated).get("/health", headers=_admin()).json()

    assert data["schema"]["revision"] == HEAD and data["schema"]["migrated_by"] == "1.5.0"


def test_health_tells_nobody_else(monkeypatch, migrated):
    client = _client(monkeypatch, migrated)
    user = {"Authorization": f"Bearer {create_access_token('user-id', 'user')}"}

    assert client.get("/health").json() == {"status": "ok", "db": "ok"}
    assert client.get("/health", headers=user).json() == {"status": "ok", "db": "ok"}


def test_with_authentication_off_everyone_is_the_admin(monkeypatch, migrated):
    assert "schema" in _client(monkeypatch, migrated, auth_enabled=False).get("/health").json()


def test_health_leaves_the_schema_out_when_the_database_is_down(monkeypatch):
    class Broken:
        def connect(self):
            raise RuntimeError("database is locked")

    data = _client(monkeypatch, Broken()).get("/health", headers=_admin()).json()

    assert data["db"] == "error" and "schema" not in data


def test_the_updates_view_carries_it_for_the_about_page(migrated):
    with Session(migrated) as session:
        view = update_check.view(session, check_enabled=True, current="1.5.0")

    assert view["schema"]["revision"] == HEAD and view["schema"]["migrated_by"] == "1.5.0"


def test_the_updates_view_has_a_null_schema_when_there_is_none():
    with Session(new_engine()) as session:  # the tables of the models, never migrated: no alembic_version
        assert update_check.view(session, check_enabled=True, current="1.5.0")["schema"] is None
