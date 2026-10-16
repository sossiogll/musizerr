"""
A stored credential is never returned, logged or printed (docs/credentials.md): not by the API
(whatever the viewer, with the key or without it), not in a log line, not in the output of the
commands, and the sealed text itself is not an API value either.

The one thing a response does carry is the value of a SECRET SETTING to an administrator
(`GET /settings/values`): that is the settings page, and the existing contract (a non-admin never
receives it); it is asserted here so a change is deliberate.
"""
from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.auth import AccountTarget
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import User
from routes import deps
from routes.auth import router as auth_router
from routes.config import router as config_router
from routes.sources import router as sources_router
from services.config_manager import ConfigManager
from services.gateway_client import GatewayClient
from services.gateway_sync import GatewaySync
from services.sources import CatalogFetcher, SourceManager

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, ApiKeyConfig, BearerHeaderRequestAuth, CredentialField, OAuth2Config
from musizerr_sdk.settings import ProviderSetting

from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry
from tests.credentials_support import THIRD_KEY, raw, use_keys
from tests.sources_support import FakeCatalogServer, FakeControlSocket, catalog, entry

API_KEY, CLIENT_SECRET, LOGIN_PASSWORD = "canary-api-key-a1", "canary-client-secret-b2", "canary-login-password-c3"
OAUTH_ACCESS, OAUTH_REFRESH, SOURCE_TOKEN, SETTING_SECRET = "canary-access-d4", "canary-refresh-e5", "canary-source-f6", "canary-setting-g7"
CREDENTIALS = [API_KEY, CLIENT_SECRET, LOGIN_PASSWORD, OAUTH_ACCESS, OAUTH_REFRESH, SOURCE_TOKEN]
SOURCE_URL = "https://private.example/catalog.json"

READ_ROUTES = [
    "/providers", "/providers/public", "/auth/linked-accounts", "/auth/status", "/auth/keyed/status", "/auth/mixed/status",
    "/sources", "/sources/providers/installed", "/settings/schema", "/settings/values",
]


class _KeyedDownload(FakeDownloadProvider):
    auth = AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.SYSTEM, api_key=ApiKeyConfig(query_param="k"))


class _ConfiguredMetadata(FakeMetadataProvider):
    """A provider that declares a secret setting of its own."""

    def config_schema(self):
        return [ProviderSetting(key="token", type="string", label="Token", default="", secret=True, required=False)]


def _registry() -> FakeRegistry:
    oauth = FakeMetadataProvider("mixed")
    oauth.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=AccountModel.PERSONAL_WITH_SHARED,
        oauth2=OAuth2Config(authorize_url="https://mixed.test/authorize", token_url="https://mixed.test/token", scopes=[],
                            request_auth=BearerHeaderRequestAuth()),
    )
    login = FakeMetadataProvider("login")
    login.auth = AuthDefinition(
        flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL,
        credential_fields=(CredentialField("username", "User"), CredentialField("password", "Password", secret=True)),
    )
    return FakeRegistry(metadata=[oauth, login, _ConfiguredMetadata("conf")], downloads=[_KeyedDownload("keyed")])


@pytest.fixture
def world(engine):
    gateway, server = FakeControlSocket(), FakeCatalogServer()
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": True})
    registry = _registry()
    app = FastAPI()
    app.include_router(auth_router, prefix="/auth")
    app.include_router(config_router)
    app.include_router(sources_router)

    def _session():
        with Session(engine) as session:
            yield session

    def _manager():
        with Session(engine) as session:
            yield SourceManager(
                session, announce_change=lambda: None,
                gateway=GatewaySync(GatewayClient(gateway.path), lambda: Session(engine)), fetcher=CatalogFetcher(server.transport),
            )

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_source_manager] = _manager

    world = type("World", (), {})()
    world.client, world.engine, world.server = TestClient(app), engine, server

    def restart() -> None:
        """A process that starts again: its caches are empty."""
        fresh = ConfigManager(engine=engine)
        app.dependency_overrides[deps.get_config] = lambda: fresh

    world.restart = restart
    with Session(engine) as session:
        users = {}
        for name, role in (("boss", "admin"), ("alice", "user")):
            user = User(username=name, password_hash="x", role=role)
            session.add(user)
            session.commit()
            session.refresh(user)
            users[name] = {"Authorization": f"Bearer {create_access_token(user.id, role)}"}
    world.admin, world.user = users["boss"], users["alice"]
    yield world
    gateway.close()


def _connect_everything(world) -> None:
    post = world.client.post
    assert post("/auth/keyed/apikey", json={"api_key": API_KEY}, headers=world.admin).status_code == 200
    assert post("/auth/mixed/client", json={"client_id": "app-id", "client_secret": CLIENT_SECRET}, headers=world.admin).status_code == 200
    assert post("/auth/login/credentials", json={"username": "ann", "password": LOGIN_PASSWORD}, headers=world.user).status_code == 200
    with Session(world.engine) as session:
        from db.auth_store import CredentialStore

        alice = session.query(User).filter(User.username == "alice").one().id
        CredentialStore(session, user_id=alice).save_oauth2_token(
            "mixed", {"access_token": OAUTH_ACCESS, "refresh_token": OAUTH_REFRESH}, AccountTarget.PERSONAL,
        )
    world.server.serve(SOURCE_URL, catalog(entry()))
    world.server.require_token[SOURCE_URL] = SOURCE_TOKEN
    assert post("/sources", json={"url": SOURCE_URL, "credential": SOURCE_TOKEN}, headers=world.admin).status_code == 201
    assert world.client.patch("/settings", json={"delegation_webhook_token": SETTING_SECRET}, headers=world.admin).status_code == 200


def _no_credential_in(text: str, *, allow=()) -> None:
    for secret in CREDENTIALS:
        assert secret not in text
    assert "enc:v1" not in text and "$sealed" not in text
    for allowed in allow:
        assert allowed in text


@pytest.mark.parametrize("viewer", ["admin", "user", None])
def test_no_response_carries_a_stored_credential(world, viewer):
    _connect_everything(world)
    headers = getattr(world, viewer) if viewer else {}

    for path in READ_ROUTES:
        response = world.client.get(path, headers=headers)
        assert response.status_code < 500, path
        _no_credential_in(response.text)
        if path == "/settings/values":
            assert (SETTING_SECRET in response.text) == (viewer == "admin"), "only an admin reads a secret setting"


@pytest.mark.parametrize("viewer", ["admin", "user"])
def test_the_responses_stay_clean_and_do_not_fail_when_the_key_is_lost(world, viewer, monkeypatch, caplog):
    _connect_everything(world)
    use_keys(monkeypatch, THIRD_KEY)
    world.restart()
    caplog.set_level(logging.INFO)
    headers = getattr(world, viewer)

    for path in READ_ROUTES:
        response = world.client.get(path, headers=headers)
        assert response.status_code < 500, f"{path}: {response.status_code}"
        _no_credential_in(response.text)
        assert SETTING_SECRET not in response.text, "an unreadable secret is not shown, sealed or not"

    accounts = {a["name"]: a for a in world.client.get("/auth/linked-accounts", headers=world.user).json()["accounts"]}
    assert accounts["login"]["personal"]["connected"] is False, "an account whose credential cannot be read is not connected"
    assert accounts["mixed"]["personal"]["connected"] is False
    keyed = next(p for p in world.client.get("/providers", headers=world.admin).json()["download"] if p["name"] == "keyed")
    assert keyed["authenticated"] is False
    sources = world.client.get("/sources", headers=world.admin).json()
    assert [s["has_credential"] for s in sources] == [False]

    for secret in (*CREDENTIALS, SETTING_SECRET, THIRD_KEY):
        assert secret not in caplog.text
    assert "CREDENTIALS_KEY" in caplog.text


def test_the_logs_of_a_normal_use_hold_no_credential(world, caplog):
    caplog.set_level(logging.DEBUG)

    _connect_everything(world)
    for path in READ_ROUTES:
        world.client.get(path, headers=world.admin)

    for secret in (*CREDENTIALS, SETTING_SECRET):
        assert secret not in caplog.text


def test_a_refused_write_does_not_echo_the_value(world):
    response = world.client.post("/auth/login/credentials", json={"username": "ann", "pin": LOGIN_PASSWORD}, headers=world.user)

    assert response.status_code == 422
    assert LOGIN_PASSWORD not in response.text


def test_the_source_commands_never_print_a_token(world, monkeypatch):
    from scripts import provider_sources

    out = io.StringIO()
    world.server.serve(SOURCE_URL, catalog(entry()))
    world.server.require_token[SOURCE_URL] = SOURCE_TOKEN
    with Session(world.engine) as session:
        code = provider_sources.main(
            ["add-source", SOURCE_URL, "--credential-env", "SOURCE_TOKEN"],
            session_factory=lambda: session,
            manager_factory=lambda s: SourceManager(
                s, announce_change=lambda: None,
                gateway=GatewaySync(GatewayClient("/nonexistent.sock"), lambda: Session(world.engine)), fetcher=CatalogFetcher(world.server.transport),
            ),
            out=out, environ={"SOURCE_TOKEN": SOURCE_TOKEN}, stdin=io.StringIO(),
        )
        provider_sources.main(
            ["list"], session_factory=lambda: session, manager_factory=lambda s: SourceManager(s), out=out, environ={}, stdin=io.StringIO(),
        )

    assert code == 0 and "token set" in out.getvalue()
    assert SOURCE_TOKEN not in out.getvalue()


def test_the_source_command_refuses_without_the_key_and_says_how(monkeypatch):
    from scripts import provider_sources

    use_keys(monkeypatch, None)
    out = io.StringIO()

    code = provider_sources.main(["list"], session_factory=lambda: None, out=out, environ={}, stdin=io.StringIO())

    assert code == 1 and "CREDENTIALS_KEY" in out.getvalue() and "Fernet.generate_key()" in out.getvalue()


def test_what_a_bad_key_error_says_never_contains_the_key():
    from db.credential_cipher import CredentialsKeyError, _build

    bad = "bad-key-canary-value-1234567890"
    with pytest.raises(CredentialsKeyError) as refused:
        _build(bad, "")

    assert bad not in str(refused.value)
    assert json.dumps(str(refused.value)).count("canary") == 0


def test_a_secret_a_provider_declares_is_shown_to_an_administrator_only_and_never_as_stored(world):
    patched = world.client.patch("/settings", json={"provider.conf.token": SETTING_SECRET}, headers=world.admin)
    assert patched.status_code == 200

    (stored,) = [row[0] for row in raw(world.engine, "SELECT value FROM settings WHERE key = 'provider.conf.token'")]
    assert SETTING_SECRET not in stored and "$sealed" in stored, "what the database holds"

    for path in ("/providers", "/settings/values"):
        admin_text = world.client.get(path, headers=world.admin).text
        assert SETTING_SECRET in admin_text, f"{path}: the settings page shows (and edits) it to an administrator"
        assert "enc:v1" not in admin_text and "$sealed" not in admin_text
        user_text = world.client.get(path, headers=world.user).text
        assert SETTING_SECRET not in user_text and "enc:v1" not in user_text and "$sealed" not in user_text
