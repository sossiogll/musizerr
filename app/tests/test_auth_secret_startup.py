"""
Sessions are signed with ``AUTH_SECRET``. With authentication enabled the
application refuses to start on a secret anyone could guess: missing, empty,
shorter than 32 characters, or one of the placeholders that ship in examples
(``change-me-in-production``, ``change-me``, ``secret``).

The check is ``user_auth.require_strong_secret``, called ONCE, from
``main.lifespan``, only when authentication is enabled. There is no switch to
skip it: the test suite runs with ``AUTH_SECRET=test`` and passes because it
never starts the application with authentication enabled (it builds apps
around the routers, or uses ``main.app`` without running its lifespan); the
tests here that do start it, set the secret they mean.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from core import user_auth
from services.config_manager import ConfigManager

from tests.database import new_engine

STRONG = "k3Qv9Zx1Lm7Tb4Rw8Ny2Hc6Pd5Sj0Fa1"  # 33 characters
EXACTLY_MINIMUM = "x" * user_auth.MIN_SECRET_LENGTH

REJECTED = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("test", id="short"),
    pytest.param("x" * (user_auth.MIN_SECRET_LENGTH - 1), id="one-short-of-the-minimum"),
    pytest.param("change-me-in-production", id="the-compose-default"),
    pytest.param("change-me", id="placeholder"),
    pytest.param("secret", id="secret"),
    pytest.param("Change-Me-In-Production", id="placeholder-in-another-case"),
    pytest.param("  change-me  ", id="placeholder-with-spaces"),
]
ACCEPTED = [
    pytest.param(STRONG, id="strong"),
    pytest.param(EXACTLY_MINIMUM, id="exactly-the-minimum"),
    pytest.param("0123456789abcdef" * 4, id="openssl-rand-hex-32"),
]


def _set_secret(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("AUTH_SECRET", raising=False)
    else:
        monkeypatch.setenv("AUTH_SECRET", value)


@pytest.mark.parametrize("secret", REJECTED)
def test_a_weak_secret_is_refused(secret, monkeypatch):
    _set_secret(monkeypatch, secret)

    with pytest.raises(user_auth.InsecureAuthSecret) as refused:
        user_auth.require_strong_secret()

    message = str(refused.value)
    assert "AUTH_SECRET" in message and "openssl rand -hex 32" in message
    assert str(user_auth.MIN_SECRET_LENGTH) in message
    if secret and len(secret.strip()) > 8:
        assert secret.strip() not in message  # the value is never echoed


@pytest.mark.parametrize("secret", ACCEPTED)
def test_a_strong_secret_is_accepted(secret, monkeypatch):
    _set_secret(monkeypatch, secret)

    user_auth.require_strong_secret()


# ---------------------------------------------------------------------------
# Startup: main.lifespan
# ---------------------------------------------------------------------------


def _run_lifespan(monkeypatch, *, auth_enabled: bool, secret, ran: list[str] | None = None) -> list[str]:
    """Runs the application's lifespan, the way uvicorn does, with ``auth_enabled`` as asked. The
    database work is not what is tested here: it is replaced by a recorder, and the list of what
    ran after the check is returned."""
    config = ConfigManager(engine=new_engine())
    config.update({"auth_enabled": auth_enabled})
    ran = [] if ran is None else ran
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "init_singletons", lambda: None)
    monkeypatch.setattr(main, "_ensure_system_user", lambda: ran.append("system user"))
    monkeypatch.setattr(main, "_bootstrap_admin", lambda: ran.append("admin bootstrap"))
    monkeypatch.setattr(main, "get_config", lambda: config)
    _set_secret(monkeypatch, secret)
    with TestClient(main.app):
        pass
    return ran


@pytest.mark.parametrize("secret", REJECTED)
def test_the_application_does_not_start_with_authentication_on_and_a_weak_secret(secret, monkeypatch):
    with pytest.raises(user_auth.InsecureAuthSecret, match="AUTH_SECRET"):
        _run_lifespan(monkeypatch, auth_enabled=True, secret=secret)


@pytest.mark.parametrize("secret", ACCEPTED)
def test_the_application_starts_with_authentication_on_and_a_strong_secret(secret, monkeypatch):
    assert _run_lifespan(monkeypatch, auth_enabled=True, secret=secret) == ["system user", "admin bootstrap"]


@pytest.mark.parametrize("secret", [None, "test", "change-me-in-production"])
def test_with_authentication_off_nothing_signs_a_session_so_the_secret_is_not_checked(secret, monkeypatch):
    assert _run_lifespan(monkeypatch, auth_enabled=False, secret=secret) == ["system user", "admin bootstrap"]


def test_a_refusal_leaves_no_trace(monkeypatch):
    """The check comes before the first write: no system user, no admin bootstrap."""
    ran: list[str] = []

    with pytest.raises(user_auth.InsecureAuthSecret):
        _run_lifespan(monkeypatch, auth_enabled=True, secret="change-me-in-production", ran=ran)

    assert ran == []
