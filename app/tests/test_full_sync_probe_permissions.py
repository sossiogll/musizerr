"""
Il risultato del probe full-sync è riservato agli admin, come il suo avvio:
riporta dettagli dell'infrastruttura (anche il testo grezzo di un errore).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.user_auth import create_access_token
from db.engine import get_session
from db.models import User
from routes import deps
from routes.config import router as config_router
from services.config_manager import ConfigManager
from workers.celery_app import celery

from tests.database import new_engine

RAW = "[Errno -3] Temporary failure in name resolution"


class _FailedResult:
    result = RuntimeError(RAW)

    def ready(self):
        return True

    def successful(self):
        return False


@pytest.fixture
def client(monkeypatch):
    engine = new_engine()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    monkeypatch.setattr(celery, "AsyncResult", lambda task_id: _FailedResult())
    app = FastAPI()
    app.include_router(config_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config

    def headers(role):
        with Session(engine) as session:
            user = User(username=role, password_hash="x", role=role)
            session.add(user)
            session.commit()
            session.refresh(user)
            return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}

    with TestClient(app) as c:
        yield c, headers


def test_a_user_cannot_read_the_probe_result(client):
    c, headers = client

    resp = c.get("/config/verify-full-sync/t1", headers=headers("user"))

    assert resp.status_code == 403
    assert RAW not in resp.text


def test_an_admin_reads_the_probe_result(client):
    c, headers = client

    resp = c.get("/config/verify-full-sync/t1", headers=headers("admin"))

    assert resp.status_code == 200
    assert resp.json()["result"]["status"] == "error"
