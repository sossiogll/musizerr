"""
The application, the workers and beat refuse to start without a usable ``CREDENTIALS_KEY``
(docs/credentials.md), before the database is touched; the compose files require it for every
service that reads the database, and for none that does not.

``user_auth.require_strong_secret`` is checked only with authentication on; this one is
unconditional: the credentials are sealed whether or not users sign in.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from db import credential_cipher
from services.config_manager import ConfigManager

from tests.credentials_support import KEY, OTHER_KEY, use_keys
from tests.database import new_engine

APP_ROOT = Path(main.__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

REFUSED = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("change-me-in-production", id="a-placeholder"),
    pytest.param("0123456789abcdef" * 4, id="not-a-fernet-key"),
    pytest.param("A" * 43 + "=", id="all-zero-key"),
]


def _run_lifespan(monkeypatch, key, ran: list[str], *, auth_enabled: bool = False) -> list[str]:
    """Runs the application's lifespan; what it ran (in place, so a refusal still leaves the record)."""
    config = ConfigManager(engine=new_engine())
    config.update({"auth_enabled": auth_enabled})
    monkeypatch.setattr(main, "require_current_schema", lambda: ran.append("schema check"))
    monkeypatch.setattr(main, "init_singletons", lambda: ran.append("singletons"))
    monkeypatch.setattr(main, "_ensure_system_user", lambda: ran.append("system user"))
    monkeypatch.setattr(main, "_bootstrap_admin", lambda: ran.append("admin bootstrap"))
    monkeypatch.setattr(main, "get_config", lambda: config)
    use_keys(monkeypatch, key)
    with TestClient(main.app):
        pass
    return ran


@pytest.mark.parametrize("auth_enabled", [False, True])
@pytest.mark.parametrize("key", REFUSED)
def test_the_application_does_not_start_without_a_usable_key_whatever_the_authentication(key, auth_enabled, monkeypatch):
    ran: list[str] = []
    with pytest.raises(credential_cipher.CredentialsKeyError, match="CREDENTIALS_KEY"):
        _run_lifespan(monkeypatch, key, ran, auth_enabled=auth_enabled)

    assert ran == [], "nothing ran: not the schema check, not the rest"


@pytest.mark.parametrize("key", [KEY, OTHER_KEY])
def test_the_application_starts_with_a_usable_key_and_migrates_first(key, monkeypatch):
    ran: list[str] = []

    _run_lifespan(monkeypatch, key, ran)

    assert ran[:2] == ["schema check", "singletons"]


def test_a_previous_key_does_not_replace_the_key(monkeypatch):
    use_keys(monkeypatch, None, OTHER_KEY)
    with pytest.raises(credential_cipher.CredentialsKeyError):
        credential_cipher.require_credentials_key()


def _import_celery_app(tmp_path, key: str | None) -> subprocess.CompletedProcess:
    """A worker, and beat, start by importing this module (`celery -A workers.celery_app.celery`)."""
    env = {k: v for k, v in os.environ.items() if k not in ("CREDENTIALS_KEY", "CREDENTIALS_KEY_PREVIOUS", "DATABASE_URL")}
    env.update(DATA_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", REDIS_URL="redis://localhost:6379/0")
    if key is not None:
        env["CREDENTIALS_KEY"] = key
    return subprocess.run(
        [sys.executable, "-c", "import workers.celery_app"], cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("key", [None, "", "not-a-key"])
def test_a_worker_and_beat_do_not_start_without_a_usable_key(tmp_path, key):
    result = _import_celery_app(tmp_path, key)

    assert result.returncode != 0
    assert "CredentialsKeyError" in result.stderr and "CREDENTIALS_KEY" in result.stderr
    if key:
        assert key not in result.stderr


def test_a_worker_and_beat_start_with_a_usable_key(tmp_path):
    result = _import_celery_app(tmp_path, KEY)

    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The compose files and the example environment
# ---------------------------------------------------------------------------

COMPOSE_FILES = ["docker-compose.yaml", "docker-compose.dev.yml"]


def _services(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not next to the application (it is not a checkout)")
    return yaml.safe_load(path.read_text())["services"]


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_every_service_that_reads_the_database_must_be_given_the_key(name):
    services = _services(name)

    readers = {n for n, s in services.items() if "DATABASE_URL" in (s.get("environment") or {})}
    assert {"init", "app"} <= readers and any(n.startswith("worker") for n in readers)
    for service in readers:
        environment = services[service]["environment"]
        assert environment["CREDENTIALS_KEY"].startswith("${CREDENTIALS_KEY:?"), f"{service}: required, like AUTH_SECRET"
        assert environment["CREDENTIALS_KEY_PREVIOUS"] == "${CREDENTIALS_KEY_PREVIOUS:-}", f"{service}: optional"


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_gateway_and_the_other_services_never_get_the_key(name):
    for service, definition in _services(name).items():
        if "DATABASE_URL" in (definition.get("environment") or {}):
            continue
        assert "CREDENTIALS_KEY" not in str(definition.get("environment")), service


def test_the_production_compose_gives_beat_the_key_too():
    environment = _services("docker-compose.yaml")["beat"]["environment"]

    assert environment["CREDENTIALS_KEY"].startswith("${CREDENTIALS_KEY:?")


def test_the_example_environment_says_how_to_make_the_key():
    path = REPO_ROOT / ".env.example"
    if not path.exists():
        pytest.skip(".env.example is not next to the application")
    text = path.read_text()

    assert "CREDENTIALS_KEY" in text and "Fernet.generate_key()" in text and "CREDENTIALS_KEY_PREVIOUS" in text
    assert "\nCREDENTIALS_KEY=" not in text, "no value is shipped, not even a placeholder"
