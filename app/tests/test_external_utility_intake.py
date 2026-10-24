"""
Real permissions (through the TestClient) and behaviour of the
``/credentials/*`` routes an external capture utility calls, on both mounts
(``/`` and ``/api``). The paths come from the provider's declaration
(``ExternalUtilityConfig.intake_name``): the host knows no provider and no
utility, so the provider here is a generic fake.

All of them are admin-only: guest 401, user 403, admin and authentication
off 200. The client secret never appears in a GET response, a rejected write
changes nothing, and a name nobody declares is a 404.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.user_auth import create_access_token
from db.auth_store import CredentialStore
from db.engine import get_session
from db.models import User
from musizeer_sdk.auth import AccountDisplayField, AccountModel, AuthDefinition, AuthFlow, ExternalUtilityConfig
from routes import deps
from routes.external_utility import router as credentials_router
from services.config_manager import ConfigManager

from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry

SECRET = "super-secret-client-value"
CLIENT_ID = "client-id-value"
NAME = "vault"
OAUTH_PROVIDER = "catalogsvc"
CAPTURE = "capture"
CREDENTIAL_TYPE = "sessions"
UTILITY = (CAPTURE, CREDENTIAL_TYPE)
MOUNTS = ("", "/api")


class _Capture(FakeDownloadProvider):
    """A provider whose accounts a utility captures, and that borrows the OAuth client of another."""

    def __init__(self, *, intake_name=NAME, borrow=OAUTH_PROVIDER, name=CAPTURE) -> None:
        super().__init__(name)
        self.auth = AuthDefinition(
            flow=AuthFlow.EXTERNAL_UTILITY, account_model=AccountModel.SYSTEM,
            external_utility=ExternalUtilityConfig(
                credential_type=CREDENTIAL_TYPE, borrow_oauth_client_from=borrow, intake_name=intake_name,
                account_display_fields=[AccountDisplayField("region", "Region")],
            ),
        )


def _seed(engine):
    with Session(engine) as session:
        store = CredentialStore(session, user_id=None)
        store.save_oauth2_client(OAUTH_PROVIDER, CLIENT_ID, SECRET)
        store.save_external_utility_account(*UTILITY, "existing", {"region": "IT", "blob_content": {"k": "v"}})
        session.commit()


def _state(engine) -> tuple:
    with Session(engine) as session:
        store = CredentialStore(session, user_id=None)
        return (
            store.get_oauth2_client(OAUTH_PROVIDER),
            store.get_external_utility_accounts(*UTILITY),
        )


def _make_user(engine, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-u", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _app(engine, config, registry) -> FastAPI:
    app = FastAPI()
    for prefix in MOUNTS:
        app.include_router(credentials_router, prefix=prefix)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    return app


@pytest.fixture(params=["guest", "user", "admin", "open"])
def mode(request, engine):
    """(mode name, header, expected status) on a database with credentials already saved."""
    _seed(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": request.param != "open"})
    app = _app(engine, config, FakeRegistry(downloads=[_Capture()]))

    headers = {}
    if request.param in ("user", "admin"):
        user = _make_user(engine, request.param)
        headers = {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    expected = {"guest": 401, "user": 403, "admin": 200, "open": 200}[request.param]
    with TestClient(app) as client:
        yield request.param, client, headers, expected


READS = [
    ("GET", f"/credentials/{NAME}_api_config", None),
    ("GET", f"/credentials/{NAME}", None),
]
WRITES = [
    ("PUT", f"/credentials/{NAME}_api_config", {"client_id": "new-id", "client_secret": "new-secret"}),
    ("POST", f"/credentials/{NAME}/intruder", {"region": "US", "blob_content": {"x": 1}}),
    ("DELETE", f"/credentials/{NAME}/existing", None),
]


@pytest.mark.parametrize("prefix", MOUNTS)
@pytest.mark.parametrize("method, path, body", READS)
def test_reads_follow_the_admin_policy_and_never_expose_the_secret(mode, prefix, method, path, body):
    _name, client, headers, expected = mode

    resp = client.request(method, prefix + path, headers=headers)

    assert resp.status_code == expected
    assert SECRET not in resp.text


@pytest.mark.parametrize("prefix", MOUNTS)
@pytest.mark.parametrize("method, path, body", WRITES)
def test_writes_follow_the_admin_policy_and_rejected_ones_change_nothing(mode, engine, prefix, method, path, body):
    _name, client, headers, expected = mode
    before = _state(engine)

    resp = client.request(method, prefix + path, json=body, headers=headers)

    assert resp.status_code == expected
    if expected != 200:
        assert _state(engine) == before


@pytest.fixture
def client(engine):
    _seed(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    with TestClient(_app(engine, config, FakeRegistry(downloads=[_Capture()]))) as test_client:
        yield test_client


def test_the_config_read_tells_the_utility_the_client_is_configured(client):
    data = client.get(f"/api/credentials/{NAME}_api_config").json()

    # The utility only checks that both fields are set.
    assert data["client_id"] == CLIENT_ID
    assert data["client_secret"] and data["client_secret"] != SECRET
    assert data["configured"] is True


def test_the_client_is_written_for_the_provider_the_declaration_names(client, engine):
    assert client.put(f"/credentials/{NAME}_api_config", json={"client_id": "n", "client_secret": "s"}).status_code == 200

    assert _state(engine)[0] == {"client_id": "n", "client_secret": "s"}


def test_the_accounts_are_listed_with_the_fields_the_provider_declares(client):
    assert client.get(f"/credentials/{NAME}").json() == [{"name": "existing", "region": "IT"}]


def test_an_account_is_kept_as_received_under_the_provider_that_declares_the_name(client, engine):
    body = {"region": "us", "blob_content": {"x": 1}, "extra": [1, 2]}

    resp = client.post(f"/credentials/{NAME}/second", json=body)

    assert resp.status_code == 200 and resp.json() == {"ok": True, "account": "second", "region": "us"}
    stored = _state(engine)[1]["second"]
    assert {k: stored[k] for k in body} == body and "registered_at" in stored


def test_an_account_without_a_name_is_refused(client):
    assert client.post(f"/credentials/{NAME}/%20", json={"a": 1}).status_code == 400


def test_a_missing_account_is_a_404(client):
    assert client.delete(f"/credentials/{NAME}/ghost").status_code == 404


def test_a_name_nobody_declares_is_a_404_whatever_the_path(client, engine):
    before = _state(engine)

    for method, path in (("GET", "/credentials/other"), ("GET", "/credentials/other_api_config"),
                         ("POST", "/credentials/other/x"), ("DELETE", "/credentials/other/existing"),
                         ("PUT", "/credentials/other_api_config")):
        resp = client.request(method, path, json={"client_id": "a", "client_secret": "b"} if method == "PUT" else {"a": 1})
        assert resp.status_code == 404, (method, path)
    assert _state(engine) == before


def test_a_provider_that_borrows_no_client_has_no_config_path(engine):
    _seed(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    registry = FakeRegistry(downloads=[_Capture(borrow=None)])
    with TestClient(_app(engine, config, registry)) as client:
        assert client.get(f"/credentials/{NAME}_api_config").status_code == 404
        assert client.get(f"/credentials/{NAME}").status_code == 200


def test_a_provider_that_declares_no_intake_serves_nothing(engine):
    _seed(engine)
    config = ConfigManager(engine=engine)
    config.update({"auth_enabled": False})
    registry = FakeRegistry(downloads=[_Capture(intake_name=None)], metadata=[FakeMetadataProvider("plain")])
    with TestClient(_app(engine, config, registry)) as client:
        assert client.get(f"/credentials/{NAME}").status_code == 404
