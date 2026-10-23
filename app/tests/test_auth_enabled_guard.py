"""
Turning ``auth_enabled`` on while the process runs with a weak ``AUTH_SECRET``
is refused where every setting is written (``SettingsService.write_values``,
through the ``guard_on`` the catalog declares), with the same rule startup asks
(``user_auth.require_strong_secret``): a 400 with the stable code
``auth_secret_insecure``, the whole batch refused, nothing written. Turning it
off is always allowed.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.settings_catalog import get_def
from db.engine import get_session
from db.models import User
from routes import deps
from routes.config import router as config_router
from services.config_manager import ConfigManager
from services.settings import SettingsService, SettingsWriteError

from tests.conftest import FakeRegistry
from tests.test_auth_secret_startup import ACCEPTED, REJECTED, STRONG, _set_secret
from tests.test_openapi_responses import assert_conforms


# An unrelated setting, to show a refused batch writes nothing at all.
FLIPPED = not get_def("save_cover").default


def _stored(engine) -> ConfigManager:
    """What is stored, read by a manager that has cached nothing."""
    return ConfigManager(engine=engine)


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


@pytest.fixture
def admin(engine) -> User:
    with Session(engine) as session:
        user = User(username="root", password_hash="x", role="admin")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def client(engine, config):
    """The settings routes with authentication off: the caller is the open-mode administrator."""
    app = FastAPI()
    app.include_router(config_router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    return TestClient(app)


def _patch(client, **values):
    return client.patch("/settings", json=values)


@pytest.mark.parametrize("secret", REJECTED)
def test_turning_authentication_on_with_a_weak_secret_is_refused_and_nothing_is_written(secret, client, engine, monkeypatch):
    _set_secret(monkeypatch, secret)

    resp = _patch(client, auth_enabled=True, save_cover=FLIPPED)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "auth_secret_insecure"
    assert "AUTH_SECRET" in detail["error_message"] and "openssl rand -hex 32" in detail["error_message"]
    if secret and len(secret.strip()) > 8:
        assert secret.strip() not in resp.text  # the value is never echoed
    assert_conforms(resp, "PATCH", "/settings")
    stored = _stored(engine)
    assert stored.auth_enabled is False
    assert stored.get("save_cover") != FLIPPED  # the whole batch was refused


@pytest.mark.parametrize("secret", ACCEPTED)
def test_turning_authentication_on_with_a_strong_secret_is_accepted(secret, client, engine, admin, monkeypatch):
    _set_secret(monkeypatch, secret)

    resp = _patch(client, auth_enabled=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_enabled"] is True
    assert _stored(engine).auth_enabled is True


@pytest.mark.parametrize("secret", [pytest.param(None, id="unset"), pytest.param("test", id="short"), STRONG])
def test_turning_authentication_off_is_always_allowed(secret, engine, config, admin, monkeypatch):
    config.update({"auth_enabled": True})
    _set_secret(monkeypatch, secret)

    with Session(engine) as session:
        SettingsService(session, config, FakeRegistry()).write_values({"auth_enabled": False}, admin)

    assert _stored(engine).auth_enabled is False


def test_a_write_that_does_not_turn_authentication_on_does_not_ask_for_the_secret(client, engine, monkeypatch):
    _set_secret(monkeypatch, "test")

    resp = _patch(client, save_cover=FLIPPED)

    assert resp.status_code == 200, resp.text
    assert _stored(engine).get("save_cover") == FLIPPED


def test_the_service_raises_a_coded_error_the_routes_pass_on(engine, config, admin, monkeypatch):
    _set_secret(monkeypatch, "secret")

    with Session(engine) as session, pytest.raises(SettingsWriteError) as refused:
        SettingsService(session, config, FakeRegistry()).write_values({"auth_enabled": True}, admin)

    assert refused.value.status_code == 400 and refused.value.error_code == "auth_secret_insecure"
    assert refused.value.detail["error_code"] == "auth_secret_insecure"


def test_the_guard_is_declared_by_the_catalog_and_never_sent_to_the_ui():
    setting = get_def("auth_enabled")

    assert setting.guard_on is not None and setting.guard_on[0] is True
    assert "guard_on" not in setting.to_public()

