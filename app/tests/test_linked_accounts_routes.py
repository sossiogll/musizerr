"""
Linked accounts over HTTP: who may connect, test and disconnect which
account of a provider, by the model the provider declares. Guest, user,
admin and authentication off; fakes only, no provider name of the product.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.auth import AccountTarget
from musizerr_sdk.auth import (
    AccountModel,
    ApiKeyConfig,
    AuthDefinition,
    AuthFlow,
    BearerHeaderRequestAuth,
    CredentialField,
    OAuth2Config,
)
from musizerr_sdk.capabilities import CAPABILITY_USER_LIBRARY
from core.user_auth import create_access_token, create_oauth_state_token
from db.auth_store import CredentialStore
from db.engine import get_session
from db.models import SYSTEM_USER_ID, User
from routes import deps
from routes.auth import router as auth_router
from routes.config import router as config_router
from services.config_manager import ConfigManager

from tests.conftest import FakeDownloadProvider, FakeMetadataProvider, FakeRegistry

PERSONAL, SHARED = AccountTarget.PERSONAL, AccountTarget.SHARED


def _oauth(name: str, model: AccountModel) -> FakeMetadataProvider:
    provider = FakeMetadataProvider(name)
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2, account_model=model,
        oauth2=OAuth2Config(authorize_url=f"https://{name}.test/authorize", token_url=f"https://{name}.test/token",
                            scopes=[], request_auth=BearerHeaderRequestAuth()),
    )
    provider.supports_user_library = lambda: True
    return provider


def _login(name: str, model: AccountModel, fields=None) -> FakeMetadataProvider:
    provider = FakeMetadataProvider(name)
    kwargs = {"credential_fields": fields} if fields else {}
    provider.auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=model, **kwargs)
    return provider


class _KeyDownload(FakeDownloadProvider):
    auth = AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.SYSTEM, api_key=ApiKeyConfig(query_param="k"))


def _no_account(name: str) -> FakeMetadataProvider:
    provider = FakeMetadataProvider(name)
    provider.auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    return provider


def _registry() -> FakeRegistry:
    return FakeRegistry(
        metadata=[
            _oauth("mixed", AccountModel.PERSONAL_WITH_SHARED),
            _oauth("solo", AccountModel.PERSONAL),
            _oauth("house", AccountModel.SYSTEM),
            _login("mixedlogin", AccountModel.PERSONAL_WITH_SHARED),
            _login("pin", AccountModel.PERSONAL, fields=(CredentialField("pin", "PIN", secret=True),)),
            _no_account("open"),
        ],
        downloads=[_KeyDownload("keyed"), FakeDownloadProvider("plain")],
    )


def _token(access: str = "t") -> dict:
    return {"access_token": access, "obtained_at": time.time(), "expires_in": 3600}


@pytest.fixture
def world(engine):
    registry = _registry()
    config = ConfigManager(engine=engine)
    app = FastAPI()
    app.include_router(auth_router, prefix="/auth")
    app.include_router(config_router)

    def _session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry

    class World:
        client = TestClient(app, follow_redirects=False)

        def user(self, name: str, role: str = "user") -> dict:
            with Session(engine) as session:
                user = User(username=name, password_hash="x", role=role)
                session.add(user)
                session.commit()
                session.refresh(user)
                self.ids[name] = user.id
                return {"Authorization": f"Bearer {create_access_token(user.id, role)}"}

        ids: dict = {}

        def open_mode(self) -> None:
            config.update({"auth_enabled": False})

        sessions: list = []

        def store(self, user: str | None = None):
            session = Session(engine)
            self.sessions.append(session)
            return CredentialStore(session, user_id=self.ids[user] if user else None)

    config.update({"auth_enabled": True})
    world = World()
    yield world
    for session in world.sessions:
        session.close()


def _by_name(accounts) -> dict:
    return {a["name"]: a for a in accounts}


# ---------------------------------------------------------------------------
# GET /auth/linked-accounts
# ---------------------------------------------------------------------------


def test_a_guest_has_no_linked_accounts(world):
    assert world.client.get("/auth/linked-accounts").status_code == 401


def test_the_list_holds_only_providers_with_personal_accounts_by_their_declared_model(world):
    alice = world.user("alice")

    accounts = world.client.get("/auth/linked-accounts", headers=alice).json()["accounts"]

    assert sorted(a["name"] for a in accounts) == ["mixed", "mixedlogin", "pin", "solo"]
    by_name = _by_name(accounts)
    assert by_name["mixed"]["account_model"] == "personal_with_shared" and by_name["solo"]["account_model"] == "personal"
    assert by_name["mixed"]["personal"] == {
        "connected": False, "username": None, "missing_permissions": [], "capabilities": [CAPABILITY_USER_LIBRARY],
        "login": None,
    }
    # The shared account is shown only where the model has one.
    assert by_name["solo"]["shared"] is None and by_name["mixed"]["shared"] == {"connected": False, "username": None}
    assert by_name["pin"]["credential_fields"] == [{"key": "pin", "label": "PIN", "secret": True}]
    assert by_name["mixed"]["credential_fields"] == []
    assert [f["key"] for f in by_name["mixedlogin"]["credential_fields"]] == ["username", "password"]


def test_an_oauth_account_is_connectable_only_once_an_admin_set_the_app_up(world):
    alice = world.user("alice")
    assert _by_name(world.client.get("/auth/linked-accounts", headers=alice).json()["accounts"])["mixed"]["connectable"] is False

    world.store().save_oauth2_client("mixed", "id", "secret")

    accounts = _by_name(world.client.get("/auth/linked-accounts", headers=alice).json()["accounts"])
    assert accounts["mixed"]["connectable"] is True and accounts["mixedlogin"]["connectable"] is True


def test_the_list_shows_what_the_viewer_connected_and_never_who_the_shared_account_is(world):
    alice = world.user("alice")
    world.store("alice").save_credentials("mixedlogin", {"username": "alice-login", "password": "pw"}, PERSONAL)
    world.store().save_credentials("mixedlogin", {"username": "house-login", "password": "pw"}, SHARED)

    accounts = _by_name(world.client.get("/auth/linked-accounts", headers=alice).json()["accounts"])

    assert accounts["mixedlogin"]["personal"]["connected"] is True
    assert accounts["mixedlogin"]["personal"]["username"] == "alice-login"
    assert accounts["mixedlogin"]["shared"] == {"connected": True, "username": None}
    assert "pw" not in str(accounts) and "house-login" not in str(accounts)


def test_one_user_never_sees_the_account_another_connected(world):
    alice, bob = world.user("alice"), world.user("bob")
    world.store("alice").save_oauth2_token("mixed", _token(), PERSONAL)

    mine = _by_name(world.client.get("/auth/linked-accounts", headers=alice).json()["accounts"])
    theirs = _by_name(world.client.get("/auth/linked-accounts", headers=bob).json()["accounts"])

    assert mine["mixed"]["personal"]["connected"] is True
    assert theirs["mixed"]["personal"]["connected"] is False


# ---------------------------------------------------------------------------
# Connect a login account
# ---------------------------------------------------------------------------


def test_a_user_connects_their_own_login_and_it_is_theirs_alone(world):
    alice = world.user("alice")
    bob = world.user("bob")

    resp = world.client.post("/auth/mixedlogin/credentials", json={"username": "u", "password": "p"}, headers=alice)

    assert resp.status_code == 200
    assert world.store("alice").own_credentials("mixedlogin").data == {"username": "u", "password": "p"}
    assert world.store("bob").own_credentials("mixedlogin") is None
    assert world.store().shared_credentials("mixedlogin") is None
    assert bob


def test_a_login_is_saved_with_the_fields_of_the_providers_form(world):
    alice = world.user("alice")

    assert world.client.post("/auth/pin/credentials", json={"username": "u", "password": "p"}, headers=alice).status_code == 422
    assert world.client.post("/auth/pin/credentials", json={"pin": ""}, headers=alice).status_code == 422
    assert world.client.post("/auth/pin/credentials", json={"pin": "1234"}, headers=alice).status_code == 200
    assert world.store("alice").own_credentials("pin").data == {"pin": "1234"}


def test_a_user_cannot_write_the_shared_account(world):
    alice = world.user("alice")

    resp = world.client.post("/auth/mixedlogin/credentials?target=shared", json={"username": "u", "password": "p"}, headers=alice)

    assert resp.status_code == 403
    assert world.store().shared_credentials("mixedlogin") is None


def test_an_admin_writes_the_shared_account_apart_from_their_own(world):
    admin = world.user("boss", "admin")

    world.client.post("/auth/mixedlogin/credentials?target=shared", json={"username": "house", "password": "p"}, headers=admin)
    world.client.post("/auth/mixedlogin/credentials", json={"username": "mine", "password": "p"}, headers=admin)

    assert world.store().shared_credentials("mixedlogin").data["username"] == "house"
    assert world.store("boss").own_credentials("mixedlogin").data["username"] == "mine"


def test_an_admins_own_connection_is_not_copied_to_the_shared_account(world):
    admin = world.user("boss", "admin")

    world.client.post("/auth/mixedlogin/credentials", json={"username": "mine", "password": "p"}, headers=admin)

    assert world.store().shared_credentials("mixedlogin") is None


def test_in_open_mode_what_the_owner_connects_is_the_one_shared_connection(world, engine):
    from tests.database import seed_system_user

    seed_system_user(engine)
    world.open_mode()

    # From Settings > Linked accounts (their own)...
    assert world.client.post("/auth/mixedlogin/credentials", json={"username": "owner", "password": "p"}).status_code == 200

    # ...it is the instance's shared account, and both pages show it.
    assert world.store().shared_credentials("mixedlogin").data["username"] == "owner"
    linked = _by_name(world.client.get("/auth/linked-accounts").json()["accounts"])["mixedlogin"]
    assert linked["personal"]["connected"] is True and linked["personal"]["username"] == "owner"
    assert linked["shared"]["connected"] is True

    # And connecting it from Administration > Providers (shared) is the same row.
    assert world.client.post(
        "/auth/mixedlogin/credentials?target=shared", json={"username": "admin-page", "password": "p"}).status_code == 200
    again = _by_name(world.client.get("/auth/linked-accounts").json()["accounts"])["mixedlogin"]
    assert again["personal"]["username"] == "admin-page"

    # Disconnecting either removes the one connection.
    assert world.client.delete("/auth/mixedlogin").status_code == 200
    assert world.store().shared_credentials("mixedlogin") is None


def test_with_authentication_on_an_admins_own_connection_is_still_not_the_shared_one(world):
    admin = world.user("boss", "admin")

    world.client.post("/auth/mixedlogin/credentials", json={"username": "mine", "password": "p"}, headers=admin)
    linked = _by_name(world.client.get("/auth/linked-accounts", headers=admin).json()["accounts"])["mixedlogin"]

    assert linked["personal"]["connected"] is True and linked["shared"]["connected"] is False


def test_a_model_without_that_account_refuses_it(world):
    admin = world.user("boss", "admin")
    for name in ("solo", "house"):
        world.store().save_oauth2_client(name, "id", "secret")

    # `solo` is personal-only: no shared account; `house` is system-only: no personal one.
    assert world.client.get("/auth/solo/authorize-url?target=shared", headers=admin).status_code == 400
    assert world.client.get("/auth/house/authorize-url?target=personal", headers=admin).status_code == 400


def test_a_login_route_refuses_a_provider_that_has_no_login(world):
    alice = world.user("alice")

    assert world.client.post("/auth/mixed/credentials", json={"username": "u", "password": "p"}, headers=alice).status_code == 400


def test_a_guest_connects_nothing(world):
    assert world.client.post("/auth/mixedlogin/credentials", json={"username": "u", "password": "p"}).status_code == 401
    assert world.client.delete("/auth/mixedlogin").status_code == 401


# ---------------------------------------------------------------------------
# OAuth2: the authorization and its callback
# ---------------------------------------------------------------------------


def test_the_authorization_url_carries_who_and_which_account(world):
    alice = world.user("alice")
    world.store().save_oauth2_client("mixed", "id", "secret")

    personal = world.client.get("/auth/mixed/authorize-url", headers=alice).json()["url"]
    from urllib.parse import parse_qs, urlparse
    from core.user_auth import decode_oauth_state_token

    state = parse_qs(urlparse(personal).query)["state"][0]
    assert decode_oauth_state_token(state) == (world.ids["alice"], "personal")
    assert world.client.get("/auth/mixed/authorize-url?target=shared", headers=alice).status_code == 403


def test_an_admin_asks_for_the_shared_authorization_and_a_system_provider_has_only_that(world):
    admin = world.user("boss", "admin")
    world.store().save_oauth2_client("house", "id", "secret")
    from urllib.parse import parse_qs, urlparse
    from core.user_auth import decode_oauth_state_token

    url = world.client.get("/auth/house/authorize-url", headers=admin).json()["url"]

    assert decode_oauth_state_token(parse_qs(urlparse(url).query)["state"][0]) == (world.ids["boss"], "shared")
    assert world.client.get("/auth/house/authorize-url?target=personal", headers=admin).status_code == 400


def test_a_user_cannot_authorize_a_system_provider(world):
    alice = world.user("alice")
    world.store().save_oauth2_client("house", "id", "secret")

    assert world.client.get("/auth/house/authorize-url", headers=alice).status_code == 403


def _granted(monkeypatch, access="granted"):
    from core.auth_manager import AuthManager

    class _Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": access, "expires_in": 3600}

    monkeypatch.setattr(AuthManager, "_oauth2_token_request", lambda self, provider, data: _Reply())


def test_the_callback_stores_the_token_as_the_account_the_state_names(world, monkeypatch):
    alice, boss = world.user("alice"), world.user("boss", "admin")
    world.store().save_oauth2_client("mixed", "id", "secret")
    _granted(monkeypatch)

    own = world.client.get("/auth/mixed/callback", params={"code": "c", "state": create_oauth_state_token(world.ids["alice"], "personal")})
    shared = world.client.get("/auth/mixed/callback", params={"code": "c", "state": create_oauth_state_token(world.ids["boss"], "shared")})

    assert own.status_code == 307 and own.headers["location"].endswith("/config/linked-accounts?auth=success")
    assert shared.headers["location"].endswith("/admin/providers?auth=success")
    assert world.store("alice").own_oauth2_token("mixed").data["access_token"] == "granted"
    assert world.store("boss").own_oauth2_token("mixed") is None
    assert world.store().shared_oauth2_token("mixed").data["access_token"] == "granted"
    assert alice and boss


def test_the_callback_rechecks_the_rule_a_forged_or_stale_state_cannot_write_the_shared_account(world, monkeypatch):
    world.user("alice")
    world.store().save_oauth2_client("mixed", "id", "secret")
    _granted(monkeypatch)

    resp = world.client.get("/auth/mixed/callback", params={"code": "c", "state": create_oauth_state_token(world.ids["alice"], "shared")})

    assert resp.status_code == 403
    assert world.store().shared_oauth2_token("mixed") is None


def test_the_callback_refuses_a_state_that_is_not_one(world):
    resp = world.client.get("/auth/mixed/callback", params={"code": "c", "state": "garbage"})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


def test_disconnecting_removes_the_row_and_the_shared_account_serves_again(world):
    alice = world.user("alice")
    world.store("alice").save_oauth2_token("mixed", _token("own"), PERSONAL)
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    assert world.client.delete("/auth/mixed", headers=alice).status_code == 200

    assert world.store("alice").own_oauth2_token("mixed") is None
    assert world.store().shared_oauth2_token("mixed") is not None
    assert world.client.get("/auth/mixed/status", headers=alice).json() == {"provider": "mixed", "authenticated": True}


def test_a_user_cannot_disconnect_the_shared_account(world):
    alice = world.user("alice")
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    assert world.client.delete("/auth/mixed?target=shared", headers=alice).status_code == 403
    assert world.client.delete("/auth/house", headers=alice).status_code == 403
    assert world.store().shared_oauth2_token("mixed") is not None


def test_an_admin_disconnects_the_shared_account_and_keeps_their_own(world):
    admin = world.user("boss", "admin")
    world.store("boss").save_oauth2_token("mixed", _token("own"), PERSONAL)
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    assert world.client.delete("/auth/mixed?target=shared", headers=admin).status_code == 200

    assert world.store().shared_oauth2_token("mixed") is None
    assert world.store("boss").own_oauth2_token("mixed") is not None


def test_the_api_key_of_the_instance_is_the_admins(world):
    alice, admin = world.user("alice"), world.user("boss", "admin")

    assert world.client.post("/auth/keyed/apikey", json={"api_key": "k"}, headers=alice).status_code == 403
    assert world.client.post("/auth/keyed/apikey", json={"api_key": "k"}, headers=admin).status_code == 200
    assert world.client.post("/auth/mixed/apikey", json={"api_key": "k"}, headers=admin).status_code == 400
    assert world.client.delete("/auth/keyed", headers=alice).status_code == 403
    assert world.store().get_api_key("keyed") == "k"
    assert world.client.delete("/auth/keyed", headers=admin).status_code == 200
    assert world.store().get_api_key("keyed") is None


def test_disconnect_all_removes_only_the_callers_own_accounts(world):
    alice, bob = world.user("alice"), world.user("bob")
    world.store("alice").save_oauth2_token("mixed", _token(), PERSONAL)
    world.store("alice").save_credentials("mixedlogin", {"username": "u", "password": "p"}, PERSONAL)
    world.store("bob").save_oauth2_token("mixed", _token(), PERSONAL)
    world.store().save_oauth2_token("mixed", _token(), SHARED)

    resp = world.client.delete("/auth/me/accounts", headers=alice)

    assert resp.json() == {"ok": True, "removed": 2}
    assert world.store("bob").own_oauth2_token("mixed") is not None
    assert world.store().shared_oauth2_token("mixed") is not None
    assert bob


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


def test_a_user_tests_their_own_account_not_the_shared_one(world):
    alice = world.user("alice")
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    own = world.client.post("/auth/mixed/test?target=personal", headers=alice).json()["account"]
    shared = world.client.post("/auth/mixed/test?target=shared", headers=alice)
    routed = world.client.post("/auth/mixed/test", headers=alice).json()["account"]

    assert own["ok"] is False  # nothing of theirs is connected
    assert shared.status_code == 403
    assert routed == {"ok": True, "detail": "Connected"}  # their calls use the shared account


def test_an_admin_tests_the_shared_account_and_the_app_client(world):
    admin = world.user("boss", "admin")
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    assert world.client.post("/auth/mixed/test?target=shared", headers=admin).json()["account"]["ok"] is True
    assert world.client.post("/auth/mixed/test?scope=app", headers=admin).json()["app"]["ok"] is False


def test_the_app_client_test_is_the_admins(world):
    alice = world.user("alice")

    assert world.client.post("/auth/mixed/test?scope=app", headers=alice).status_code == 403


# ---------------------------------------------------------------------------
# GET /providers: the model and the state of each account
# ---------------------------------------------------------------------------


def _providers(world, headers=None) -> dict:
    data = world.client.get("/providers", headers=headers or {}).json()
    return {p["name"]: p for category in data.values() for p in category}


def test_the_provider_list_says_the_model_and_the_state_of_each_account(world):
    admin = world.user("boss", "admin")
    world.store("boss").save_credentials("mixedlogin", {"username": "mine", "password": "p"}, PERSONAL)
    world.store().save_credentials("mixedlogin", {"username": "house", "password": "p"}, SHARED)

    providers = _providers(world, admin)

    assert {name: p["account_model"] for name, p in providers.items()} == {
        "mixed": "personal_with_shared", "solo": "personal", "house": "system", "mixedlogin": "personal_with_shared",
        "pin": "personal", "open": "none", "keyed": "system", "plain": "none",
    }
    assert providers["mixedlogin"]["personal"]["username"] == "mine"
    assert providers["mixedlogin"]["shared"] == {"connected": True, "username": "house"}
    assert providers["solo"]["shared"] is None and providers["house"]["personal"] is None
    assert providers["open"]["personal"] is None and providers["open"]["shared"] is None
    assert [f["key"] for f in providers["mixedlogin"]["credential_fields"]] == ["username", "password"]


def test_a_user_never_learns_who_the_shared_account_is(world):
    alice = world.user("alice")
    world.store().save_credentials("mixedlogin", {"username": "instance-login", "password": "p"}, SHARED)

    providers = _providers(world, alice)

    assert providers["mixedlogin"]["shared"] == {"connected": True, "username": None}
    assert "instance-login" not in str(providers)


def test_a_system_account_is_connected_when_the_instance_has_it(world):
    admin = world.user("boss", "admin")
    world.store().save_api_key("keyed", "k")

    providers = _providers(world, admin)

    assert providers["keyed"]["shared"] == {"connected": True, "username": None}
    assert providers["keyed"]["authenticated"] is True


def test_the_viewers_calls_authenticate_by_the_rule(world):
    alice = world.user("alice")
    world.store().save_oauth2_token("solo", _token("shared-row"), SHARED)
    world.store().save_oauth2_token("mixed", _token("shared"), SHARED)

    providers = _providers(world, alice)

    assert providers["mixed"]["authenticated"] is True  # the shared account serves them
    assert providers["solo"]["authenticated"] is False  # a personal-only provider never uses it
