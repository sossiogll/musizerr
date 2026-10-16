"""
Confine di permesso reale (via TestClient, non chiamata diretta della route)
per ``/system/routines*``: admin-only (``AdminUserDep``), quindi:

  - anonimo, con ``auth_enabled=True``           -> 401 (nessuna credenziale)
  - utente autenticato non-admin                  -> 403 (require_admin)
  - admin                                         -> 200

Diverso deliberatamente dallo stile "chiama la route come funzione Python"
usato altrove (``test_config_route.py``, ``test_history_permissions.py``):
quello stile bypassa proprio la catena di dipendenze FastAPI
(``get_current_user``/``require_admin``) che qui è l'unica cosa sotto test —
va quindi montata su un'app FastAPI reale e interrogata via HTTP.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from services.config_manager import ConfigManager
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import User
from routes import deps
from routes.system.routines import router as routines_router



@pytest.fixture
def client(engine, monkeypatch):
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})

    app = FastAPI()
    app.include_router(routines_router)

    def _session_override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[deps.get_config] = lambda: config
    # Mai usato dai path sotto test (include_providers=False in tutte le
    # chiamate a SettingsService da routes/system/routines.py) — un None
    # basta, evita di istanziare un ProviderRegistry reale (discovery).
    app.dependency_overrides[deps.get_registry] = lambda: None

    with TestClient(app) as c:
        yield c, engine


def _make_user(engine, *, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-user", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _token_for(user: User) -> str:
    return create_access_token(user.id, user.role)


def test_anonymous_get_is_401_when_auth_enabled(client):
    c, _engine = client
    resp = c.get("/system/routines")
    assert resp.status_code == 401


def test_non_admin_get_is_403(client):
    c, engine = client
    user = _make_user(engine, role="user")
    resp = c.get("/system/routines", headers={"Authorization": f"Bearer {_token_for(user)}"})
    assert resp.status_code == 403


def test_admin_get_is_200(client):
    c, engine = client
    admin = _make_user(engine, role="admin")
    resp = c.get("/system/routines", headers={"Authorization": f"Bearer {_token_for(admin)}"})
    assert resp.status_code == 200
    assert "routines" in resp.json()


def test_non_admin_patch_is_403(client):
    c, engine = client
    user = _make_user(engine, role="user")
    resp = c.patch(
        "/system/routines/sweep_delegations",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {_token_for(user)}"},
    )
    assert resp.status_code == 403


def test_non_admin_run_now_is_403(client):
    c, engine = client
    user = _make_user(engine, role="user")
    resp = c.post(
        "/system/routines/sweep_delegations/run",
        headers={"Authorization": f"Bearer {_token_for(user)}"},
    )
    assert resp.status_code == 403


def test_anonymous_patch_is_401(client):
    c, _engine = client
    resp = c.patch("/system/routines/sweep_delegations", json={"enabled": False})
    assert resp.status_code == 401
