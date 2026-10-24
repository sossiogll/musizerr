"""
``GET /health``: chiunque ottiene esattamente ``{status, db}``; un admin (o
chiunque con l'autenticazione disattivata) anche l'elenco dei provider e il
dettaglio di un errore del DB. Un DB irraggiungibile è riportato a entrambi.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.user_auth import create_access_token, create_stream_token
from routes import deps
from routes.system import health
from routes.system.health import router as health_router
from services.config_manager import ConfigManager

from tests.database import new_engine
from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry


class _BrokenEngine:
    def connect(self):
        raise RuntimeError("database is locked at /data/musizeer.db")


@pytest.fixture
def client_for(monkeypatch):
    def build(*, auth_enabled: bool = True, db_up: bool = True) -> TestClient:
        settings_engine = new_engine()
        config = ConfigManager(engine=settings_engine)
        config.update({"auth_enabled": auth_enabled})
        monkeypatch.setattr(health, "engine", settings_engine if db_up else _BrokenEngine())
        registry = FakeRegistry(metadata=[FakeMetadataProvider("m1")], downloads=[FakeDownloadProvider("d1")])
        monkeypatch.setattr(health, "get_registry", lambda: registry)
        app = FastAPI()
        app.include_router(health_router)
        app.dependency_overrides[deps.get_config] = lambda: config
        return TestClient(app)
    return build


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token('admin-id', 'admin')}"}


def test_a_guest_gets_exactly_status_and_db(client_for):
    assert client_for().get("/health").json() == {"status": "ok", "db": "ok"}


def test_a_signed_in_user_gets_exactly_status_and_db(client_for):
    headers = {"Authorization": f"Bearer {create_access_token('user-id', 'user')}"}

    assert client_for().get("/health", headers=headers).json() == {"status": "ok", "db": "ok"}


def test_an_admin_also_gets_the_providers(client_for):
    data = client_for().get("/health", headers=_admin_headers()).json()

    assert data["status"] == "ok" and data["db"] == "ok"
    assert data["providers"] == {"metadata": ["m1"], "download": ["d1"]}


def test_with_auth_disabled_everyone_gets_the_providers(client_for):
    assert "providers" in client_for(auth_enabled=False).get("/health").json()


def test_a_stream_token_is_not_an_admin_token(client_for):
    headers = {"Authorization": f"Bearer {create_stream_token('admin-id')}"}

    assert "providers" not in client_for().get("/health", headers=headers).json()


def test_a_db_failure_is_reported_to_a_guest_without_the_raw_error(client_for):
    resp = client_for(db_up=False).get("/health")

    assert resp.json() == {"status": "degraded", "db": "error"}
    assert "/data/musizeer.db" not in resp.text


def test_a_db_failure_is_reported_to_an_admin_with_the_detail(client_for):
    data = client_for(db_up=False).get("/health", headers=_admin_headers()).json()

    assert data["status"] == "degraded" and data["db"] == "error"
    assert "database is locked" in data["db_detail"]
    assert "providers" in data
