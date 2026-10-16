"""
Signing in through a provider the user linked, over HTTP: who is listed, how
the browser goes out and comes back (state bound to the browser by a nonce, a
one-time code instead of a session in a URL), who signs in (the user the
provider's id is linked to, never matched by name or email), what a first
sign-in does under each policy, how an account is linked from an open session,
and the limit on the routes. Fakes only: no provider name of the product.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import provider_login as rules
from core import user_auth
from core.auth import AccountTarget
from musizerr_sdk.auth import AccountModel
from core.auth_manager import AuthManager
from musizerr_sdk.capabilities import CAPABILITY_LOGIN
from core.interfaces import personal_capabilities
from core.settings_catalog import CATALOG
from core.user_auth import create_access_token
from db import credential_cipher, user_service
from db.auth_store import CredentialStore
from db.engine import get_session
from db.models import ProviderCredential, ProviderLoginCode, RefreshToken, User
from routes import deps
from routes.admin import router as admin_router
from routes.auth import router as auth_router
from routes.errors import install_error_handlers
from routes.provider_login import router as provider_login_router
from routes.users_auth import router as users_auth_router
from services import login_rate_limit, provider_login
from services.linked_accounts import linked_accounts
from services.config_manager import ConfigManager

from tests.conftest import FakeRedis, FakeRegistry
from tests.provider_login_support import FormLoginProvider, OAuthLoginProvider

PERSONAL = AccountTarget.PERSONAL
NONCE_COOKIE = user_auth.LOGIN_NONCE_COOKIE_NAME


def _providers() -> dict:
    """The providers of the test, by name (the provider side: what a test sets on
    them); the host gets their proxies from the registry."""
    return {p.name: p for p in (
        OAuthLoginProvider("orbit"),
        OAuthLoginProvider("plain", login=False),
        OAuthLoginProvider("house", AccountModel.SYSTEM),
        FormLoginProvider("gate"),
        FormLoginProvider("plainform", login=False),
    )}


class _Reply:
    status_code = 200

    def __init__(self, token: str) -> None:
        self._token = token

    def json(self):
        return {"access_token": self._token, "expires_in": 3600}


@pytest.fixture
def world(engine, monkeypatch):
    providers = _providers()
    registry = FakeRegistry(metadata=list(providers.values()))
    config = ConfigManager(engine=engine)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(provider_login_router, prefix="/auth/account")
    app.include_router(users_auth_router, prefix="/auth/account")
    app.include_router(admin_router, prefix="/admin")
    app.include_router(auth_router, prefix="/auth")

    def _session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    config.update({"auth_enabled": True})
    granted = {"token": "nobody"}
    # The code the callback exchanges is worth the access token the test chose: the id of the account.
    monkeypatch.setattr(AuthManager, "_oauth2_token_request", lambda self, provider, data: _Reply(granted["token"]))

    class World:
        engine_ = engine
        registry_ = registry
        providers_ = providers
        config_ = config
        client = TestClient(app, follow_redirects=False)
        ids: dict = {}

        def browser(self) -> TestClient:
            return TestClient(app, follow_redirects=False)

        def grant(self, token: str) -> None:
            """What the provider hands the next code exchange: here the access token IS the account's id."""
            granted["token"] = token

        def policy(self, value: str) -> None:
            config.update({rules.POLICY_SETTING_KEY: value})

        def set_up_oauth_app(self) -> None:
            with Session(engine) as session:
                CredentialStore(session).save_oauth2_client("orbit", "id", "secret")

        def user(self, name: str, role: str = "user", *, active: bool = True, email: str | None = None) -> dict:
            with Session(engine) as session:
                user = User(username=name, password_hash="x", role=role, is_active=active, email=email)
                session.add(user)
                session.commit()
                session.refresh(user)
                self.ids[name] = user.id
                return {"Authorization": f"Bearer {create_access_token(user.id, role)}"}

        def link(self, name: str, provider: str, external_id: str) -> None:
            with Session(engine) as session:
                store = CredentialStore(session, user_id=self.ids[name])
                if provider == "gate":
                    store.save_credentials(provider, {"username": external_id, "password": "good"}, PERSONAL, external_id=external_id)
                else:
                    store.save_oauth2_token(provider, {"access_token": external_id}, PERSONAL, external_id=external_id)

        def users(self) -> list[User]:
            with Session(engine) as session:
                return list(session.exec(select(User).order_by(User.username)).all())

        def credentials(self, provider: str) -> list[ProviderCredential]:
            with Session(engine) as session:
                return list(session.exec(select(ProviderCredential).where(
                    ProviderCredential.provider == provider, ProviderCredential.user_id.is_not(None),
                )).all())

        def go_out(self, browser: TestClient | None = None, provider: str = "orbit") -> str:
            """The browser starts a sign-in: the state it carries back."""
            browser = browser or self.client
            started = browser.get(f"/auth/account/login/{provider}/start")
            assert started.status_code == 307
            return parse_qs(urlparse(started.headers["location"]).query)["state"][0]

        def come_back(self, token: str, state: str, browser: TestClient | None = None, *, provider: str = "orbit",
                      code: str | None = "c") -> dict[str, str]:
            """The provider sends the browser back having granted ``token``: where it lands (query of the redirect)."""
            self.grant(token)
            params = {"state": state, **({"code": code} if code else {})}
            resp = (browser or self.client).get(f"/auth/{provider}/callback", params=params)
            assert resp.status_code == 307, resp.text
            landing = urlparse(resp.headers["location"])
            assert landing.path == "/login/provider"
            return {k: v[0] for k, v in parse_qs(landing.query).items()}

        def sign_in(self, token: str, browser: TestClient | None = None) -> dict[str, str]:
            browser = browser or self.client
            return self.come_back(token, self.go_out(browser), browser)

        def exchange(self, code: str):
            return self.client.post("/auth/account/login/exchange", json={"code": code})

        def sign_in_form(self, username: str, password: str = "good"):
            return self.client.post(
                "/auth/account/login/gate/credentials", json={"username": username, "password": password},
            )

    world = World()
    world.ids = {}
    world.set_up_oauth_app()
    return world


def _sub(token_response) -> str:
    return user_auth.decode_access_token(token_response.json()["access_token"])["sub"]


# ---------------------------------------------------------------------------
# Which providers
# ---------------------------------------------------------------------------


def test_the_list_holds_the_providers_that_declare_login_and_have_accounts_of_a_users_own(world):
    listed = world.client.get("/auth/account/login-providers").json()["providers"]

    # "plain" and "plainform" do not declare it; "house" declares it but its only account is the instance's.
    assert [p["name"] for p in listed] == ["orbit", "gate"]
    by_name = {p["name"]: p for p in listed}
    assert by_name["orbit"] == {"name": "orbit", "label": "Orbit", "auth_flow": "oauth2", "credential_fields": []}
    assert by_name["gate"]["auth_flow"] == "credentials"
    assert by_name["gate"]["credential_fields"] == [
        {"key": "username", "label": "Username", "secret": False},
        {"key": "password", "label": "Password", "secret": True},
    ]


def test_an_oauth_provider_is_listed_only_once_an_admin_set_its_app_up(world):
    with Session(world.engine_) as session:
        CredentialStore(session).delete_oauth2_client("orbit")

    assert [p["name"] for p in world.client.get("/auth/account/login-providers").json()["providers"]] == ["gate"]


def test_nothing_is_listed_with_authentication_off(world):
    world.config_.update({"auth_enabled": False})

    assert world.client.get("/auth/account/login-providers").json() == {"providers": []}


def test_a_provider_that_declares_login_adds_the_capability_to_its_personal_accounts(world):
    by_name = {p.name: p for p in world.registry_.all_metadata()}

    assert personal_capabilities(by_name["orbit"]) == [CAPABILITY_LOGIN]
    assert personal_capabilities(by_name["plain"]) == []
    # A shared-only provider can declare it, and still nobody signs in through the instance's own account.
    assert personal_capabilities(by_name["house"]) == []


# ---------------------------------------------------------------------------
# The way out: the state is bound to the browser
# ---------------------------------------------------------------------------


def test_the_start_sends_the_browser_to_the_provider_with_a_state_and_a_nonce_cookie(world):
    resp = world.client.get("/auth/account/login/orbit/start")

    location = urlparse(resp.headers["location"])
    assert resp.status_code == 307 and location.netloc == "orbit.test" and location.path == "/authorize"
    state = parse_qs(location.query)["state"][0]
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{NONCE_COOKIE}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
    nonce = world.client.cookies[NONCE_COOKIE]
    user_auth.verify_login_state_token(state, provider="orbit", nonce=nonce)  # does not raise


@pytest.mark.parametrize("provider, status", [("plain", 404), ("house", 404), ("nobody", 404), ("gate", 400)])
def test_the_start_refuses_what_cannot_sign_in_through_an_authorization(world, provider, status):
    assert world.client.get(f"/auth/account/login/{provider}/start").status_code == status


def test_the_start_refuses_with_authentication_off(world):
    world.config_.update({"auth_enabled": False})

    assert world.client.get("/auth/account/login/orbit/start").status_code == 400


def test_the_callback_refuses_a_state_without_the_cookie_of_the_browser_that_started_it(world):
    state = world.go_out()
    elsewhere = world.browser()  # another browser: it holds no nonce

    landed = world.come_back("alice:ext-1", state, elsewhere)

    assert landed == {"error": rules.ERROR_INVALID_STATE}


def test_the_callback_refuses_a_state_that_belongs_to_another_browser(world):
    victim, attacker = world.browser(), world.browser()
    attackers_state = world.go_out(attacker)
    world.go_out(victim)  # the victim holds a nonce of its own

    landed = world.come_back("alice:ext-1", attackers_state, victim)

    assert landed == {"error": rules.ERROR_INVALID_STATE}


def test_the_callback_refuses_a_state_started_for_another_provider(world):
    state = world.go_out(provider="orbit")

    landed = world.come_back("alice:ext-1", state, provider="gate")

    assert landed == {"error": rules.ERROR_INVALID_STATE}


def test_the_callback_refuses_a_state_that_is_not_one(world):
    resp = world.client.get("/auth/orbit/callback", params={"code": "c", "state": "garbage"})

    assert resp.status_code == 400


def test_the_nonce_cookie_is_cleared_when_the_browser_lands(world):
    state = world.go_out()
    world.policy(rules.POLICY_ACTIVE)

    resp = world.client.get("/auth/orbit/callback", params={"code": "c", "state": state})

    assert f'{NONCE_COOKIE}="";' in resp.headers["set-cookie"] or f"{NONCE_COOKIE}=;" in resp.headers["set-cookie"]


# ---------------------------------------------------------------------------
# Who signs in: the user the provider's id is linked to
# ---------------------------------------------------------------------------


def test_the_user_an_account_is_linked_to_signs_in_and_gets_a_session_through_a_one_time_code(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")

    landed = world.sign_in("anything:ext-alice")

    assert set(landed) == {"code"}
    session = world.exchange(landed["code"])
    assert session.status_code == 200
    assert _sub(session) == world.ids["alice"]
    # The same issuing as a password login: a refresh token of its own family, which rotates like any.
    refreshed = world.client.post("/auth/account/refresh", json={"refresh_token": session.json()["refresh_token"]})
    assert refreshed.status_code == 200 and _sub(refreshed) == world.ids["alice"]


def test_a_code_works_once(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")
    code = world.sign_in("x:ext-alice")["code"]

    first, second = world.exchange(code), world.exchange(code)

    assert (first.status_code, second.status_code) == (200, 401)
    assert second.json()["detail"]["error_code"] == rules.ERROR_INVALID_CODE
    with Session(world.engine_) as session:
        assert len(session.exec(select(RefreshToken)).all()) == 1  # the second exchange issued nothing


def test_a_code_expires_and_an_unknown_one_is_refused(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")
    with Session(world.engine_) as session:
        user = user_service.get_user_by_id(session, world.ids["alice"])
        now = datetime.now(timezone.utc)
        code = provider_login.issue_code(session, user, now=now)

        with pytest.raises(rules.LoginRefused) as late:
            provider_login.exchange_code(session, code, now=now + timedelta(seconds=user_auth.LOGIN_CODE_EXPIRE_SECONDS + 1))

    assert late.value.error_code == rules.ERROR_INVALID_CODE
    assert world.exchange("never-issued").status_code == 401


def test_only_the_hash_of_a_code_is_stored(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")
    code = world.sign_in("x:ext-alice")["code"]

    with Session(world.engine_) as session:
        stored = session.exec(select(ProviderLoginCode)).one()

    assert stored.code_hash == user_auth.hash_refresh_token(code) and code not in (stored.code_hash, stored.user_id)


def test_a_user_deactivated_after_the_sign_in_gets_no_session_for_the_code(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")
    code = world.sign_in("x:ext-alice")["code"]
    with Session(world.engine_) as session:
        user_service.update_user(session, user_service.get_user_by_id(session, world.ids["alice"]), is_active=False)

    resp = world.exchange(code)

    assert resp.status_code == 403 and resp.json()["detail"]["error_code"] == rules.ERROR_DEACTIVATED


def test_the_token_the_provider_just_granted_replaces_the_stored_one(world):
    world.user("alice")
    world.link("alice", "orbit", "ext-alice")

    world.sign_in("fresh:ext-alice")

    (row,) = world.credentials("orbit")
    assert row.external_id == "ext-alice" and '"access_token": "fresh:ext-alice"' in credential_cipher.unseal(row.data, "the stored token")


def test_a_deactivated_user_cannot_sign_in(world):
    world.user("alice", active=False)
    world.link("alice", "orbit", "ext-alice")

    assert world.sign_in("x:ext-alice") == {"error": rules.ERROR_DEACTIVATED}


def test_a_user_who_refused_at_the_provider_lands_on_the_error_page(world):
    state = world.go_out()

    landed = world.come_back("x", state, code=None)

    assert landed == {"error": rules.ERROR_REFUSED}


def test_an_unreachable_provider_lands_on_the_error_page_and_creates_nothing(world):
    world.policy(rules.POLICY_ACTIVE)
    world.providers_["orbit"].unreachable = True

    assert world.sign_in("x:ext-alice") == {"error": rules.ERROR_UNAVAILABLE}
    assert world.users() == []


def test_a_provider_that_does_not_know_the_account_refuses_it(world):
    world.policy(rules.POLICY_ACTIVE)

    assert world.sign_in("unknown") == {"error": rules.ERROR_REFUSED}
    assert world.users() == []


def test_a_sign_in_with_authentication_off_is_refused(world):
    state = world.go_out()
    world.config_.update({"auth_enabled": False})

    assert world.come_back("x:ext-alice", state) == {"error": rules.ERROR_REFUSED}


# ---------------------------------------------------------------------------
# The first sign-in of an account nobody linked: the admin's policy
# ---------------------------------------------------------------------------


def test_the_policy_is_a_system_setting_that_defaults_to_the_most_restrictive_value(world):
    entry = next(e for e in CATALOG if e.key == rules.POLICY_SETTING_KEY)

    assert (entry.scope, entry.admin_only, entry.type, entry.group) == ("system", True, "enum", "auth")
    assert entry.options == (rules.POLICY_DISABLED, rules.POLICY_PENDING, rules.POLICY_ACTIVE)
    assert entry.default == rules.POLICY_DISABLED == entry.fail_safe
    assert world.config_.provider_login_new_accounts == rules.POLICY_DISABLED


def test_by_default_an_account_nobody_linked_is_refused_and_nothing_is_created(world):
    assert world.sign_in("alice:ext-alice") == {"error": rules.ERROR_DISABLED}

    assert world.users() == [] and world.credentials("orbit") == []


def test_with_the_pending_policy_an_account_is_created_inactive_and_waits_for_an_admin(world):
    world.policy(rules.POLICY_PENDING)
    admin = world.user("boss", "admin")

    assert world.sign_in("Alice Smith:ext-alice") == {"error": rules.ERROR_PENDING}

    created = next(u for u in world.users() if u.username == "alice-smith")
    assert (created.role, created.is_active, created.pending_approval) == ("user", False, True)
    (row,) = world.credentials("orbit")
    assert (row.user_id, row.external_id) == (created.id, "ext-alice")
    # Signing in again changes nothing: it still waits, and no second account appears.
    assert world.sign_in("Alice Smith:ext-alice") == {"error": rules.ERROR_PENDING}
    assert len(world.users()) == 2
    listed = world.client.get("/admin/users", headers=admin).json()
    assert [u["pending_approval"] for u in listed["items"] if u["username"] == "alice-smith"] == [True]

    # An admin activating the account approves it: it signs in from then on.
    patched = world.client.patch(f"/admin/users/{created.id}", json={"is_active": True}, headers=admin)
    assert patched.status_code == 200 and (patched.json()["is_active"], patched.json()["pending_approval"]) == (True, False)
    assert set(world.sign_in("Alice Smith:ext-alice")) == {"code"}


def test_with_the_active_policy_an_account_is_created_and_signed_in_at_once(world):
    world.policy(rules.POLICY_ACTIVE)

    landed = world.sign_in("Alice Smith:ext-alice")

    session = world.exchange(landed["code"])
    created = next(u for u in world.users() if u.username == "alice-smith")
    assert session.status_code == 200 and _sub(session) == created.id
    assert (created.role, created.is_active, created.pending_approval) == ("user", True, False)
    # The second sign-in is the same user, not a second account.
    assert _sub(world.exchange(world.sign_in("Alice Smith:ext-alice")["code"])) == created.id
    assert len(world.users()) == 1


def test_the_admin_role_never_comes_from_a_provider(world):
    world.policy(rules.POLICY_ACTIVE)

    world.sign_in("admin:ext-boss")

    (created,) = world.users()
    assert created.username == "admin" and created.role == "user"
    # And the account nobody knows the password of cannot be entered by the password route.
    login = world.client.post("/auth/account/login", json={"username": "admin", "password": ""})
    assert login.status_code == 401


def test_an_account_is_never_matched_to_a_user_by_name_or_email(world):
    world.policy(rules.POLICY_ACTIVE)
    world.user("alice", email="alice@example.test")

    landed = world.sign_in("alice:ext-someone-else")

    created = {u.username: u for u in world.users()}
    assert sorted(created) == ["alice", "alice-2"]
    assert _sub(world.exchange(landed["code"])) == created["alice-2"].id
    assert world.credentials("orbit")[0].user_id == created["alice-2"].id  # nothing was linked to "alice"


def test_an_account_linked_to_one_user_never_signs_in_another(world):
    world.policy(rules.POLICY_ACTIVE)
    world.user("alice")
    world.user("bob")
    world.link("alice", "orbit", "ext-alice")

    landed = world.sign_in("bob:ext-alice")

    assert _sub(world.exchange(landed["code"])) == world.ids["alice"]


# ---------------------------------------------------------------------------
# A login account
# ---------------------------------------------------------------------------


def test_the_user_a_login_account_is_linked_to_signs_in_with_it(world):
    world.user("alice")
    world.link("alice", "gate", "id-alice")

    resp = world.sign_in_form("alice")

    assert resp.status_code == 200 and _sub(resp) == world.ids["alice"]
    refreshed = world.client.post("/auth/account/refresh", json={"refresh_token": resp.json()["refresh_token"]})
    assert refreshed.status_code == 200


def test_a_login_account_the_provider_refuses_does_not_sign_in(world):
    world.user("alice")
    world.link("alice", "gate", "id-alice")

    resp = world.sign_in_form("alice", "wrong")

    assert resp.status_code == 401 and resp.json()["detail"]["error_code"] == rules.ERROR_REFUSED


def test_a_login_account_nobody_linked_follows_the_policy(world):
    assert world.sign_in_form("carol").json()["detail"]["error_code"] == rules.ERROR_DISABLED
    assert world.users() == []

    world.policy(rules.POLICY_PENDING)
    pending = world.sign_in_form("carol")
    assert (pending.status_code, pending.json()["detail"]["error_code"]) == (403, rules.ERROR_PENDING)
    assert [(u.username, u.is_active, u.pending_approval) for u in world.users()] == [("carol", False, True)]

    world.policy(rules.POLICY_ACTIVE)
    active = world.sign_in_form("dave")
    assert active.status_code == 200
    assert _sub(active) == next(u.id for u in world.users() if u.username == "dave")
    # The credentials that proved it are the new account's own, with the id the provider gave.
    stored = {row.external_id: row for row in world.credentials("gate")}
    assert sorted(stored) == ["id-carol", "id-dave"] and '"password": "good"' in credential_cipher.unseal(stored["id-dave"].data, "the stored login")


def test_a_login_form_must_be_exactly_the_fields_the_provider_declares(world):
    resp = world.client.post("/auth/account/login/gate/credentials", json={"username": "alice"})

    assert resp.status_code == 422


def test_an_unreachable_provider_is_a_retryable_failure_not_a_500(world):
    world.policy(rules.POLICY_ACTIVE)

    resp = world.sign_in_form("down")

    assert resp.status_code == 503 and world.users() == []


def test_the_credentials_route_only_serves_a_provider_with_a_login_form(world):
    assert world.client.post("/auth/account/login/orbit/credentials", json={"username": "a", "password": "b"}).status_code == 400
    assert world.client.post("/auth/account/login/plainform/credentials", json={"username": "a", "password": "b"}).status_code == 404


# ---------------------------------------------------------------------------
# Linking: only from an open session
# ---------------------------------------------------------------------------


def test_linking_a_login_account_from_a_session_stores_the_id_the_provider_gave(world):
    alice = world.user("alice")

    saved = world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=alice)

    assert saved.status_code == 200
    (row,) = world.credentials("gate")
    assert (row.user_id, row.external_id) == (world.ids["alice"], "id-alice")
    assert world.sign_in_form("alice").status_code == 200


def test_the_linked_accounts_say_whether_the_user_can_sign_in_through_each(world):
    alice = world.user("alice")
    world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=alice)
    # Linked before sign-in existed: no id, so it cannot sign anybody in until reconnected.
    with Session(world.engine_) as session:
        CredentialStore(session, user_id=world.ids["alice"]).save_oauth2_token("orbit", {"access_token": "t"}, PERSONAL)
    with Session(world.engine_) as session:
        by_name = {a["name"]: a["personal"] for a in linked_accounts(world.registry_, session, world.ids["alice"])}

    assert by_name["gate"]["login"] is True and by_name["gate"]["capabilities"] == [CAPABILITY_LOGIN]
    assert by_name["orbit"]["login"] is False
    assert by_name["plain"]["login"] is None


def test_an_account_another_user_linked_cannot_be_linked_again(world):
    alice, bob = world.user("alice"), world.user("bob")
    world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=alice)

    taken = world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=bob)

    assert taken.status_code == 409
    assert [row.user_id for row in world.credentials("gate")] == [world.ids["alice"]]
    # The same user linking it again is a reconnection, not a conflict.
    assert world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=alice).status_code == 200


def test_a_login_account_the_provider_does_not_recognise_is_not_linked(world):
    alice = world.user("alice")

    refused = world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "wrong"}, headers=alice)

    assert refused.status_code == 422 and world.credentials("gate") == []


def test_a_provider_that_cannot_sign_in_links_with_no_id_asked(world):
    alice = world.user("alice")

    saved = world.client.post("/auth/plainform/credentials", json={"username": "a", "password": "anything"}, headers=alice)

    assert saved.status_code == 200
    assert [row.external_id for row in world.credentials("plainform")] == [None]


def test_linking_an_oauth_account_from_a_session_stores_the_id_and_refuses_one_already_linked(world):
    world.user("alice")
    world.user("bob")

    def link(user: str, account: str):
        # The linking callback needs no cookie: its state carries the signed-in user.
        world.grant(account)
        return world.client.get("/auth/orbit/callback", params={
            "code": "c", "state": user_auth.create_oauth_state_token(world.ids[user], "personal"),
        })

    assert link("alice", "ext-alice").status_code == 307
    assert [(r.user_id, r.external_id) for r in world.credentials("orbit")] == [(world.ids["alice"], "ext-alice")]

    assert link("bob", "ext-alice").status_code == 409
    assert [r.user_id for r in world.credentials("orbit")] == [world.ids["alice"]]


def test_disconnecting_the_account_ends_the_sign_in_through_it(world):
    alice = world.user("alice")
    world.client.post("/auth/gate/credentials", json={"username": "alice", "password": "good"}, headers=alice)
    assert world.sign_in_form("alice").status_code == 200

    assert world.client.delete("/auth/gate", headers=alice).status_code == 200

    assert world.sign_in_form("alice").json()["detail"]["error_code"] == rules.ERROR_DISABLED


def test_the_id_of_an_account_is_unique_per_provider(world):
    world.user("alice")
    world.user("bob")
    world.link("alice", "orbit", "ext-1")
    world.link("alice", "gate", "ext-1")  # the same id on another provider is another account

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        world.link("bob", "orbit", "ext-1")
    with Session(world.engine_) as session:
        assert CredentialStore(session).linked_user_id("orbit", "ext-1") == world.ids["alice"]


# ---------------------------------------------------------------------------
# The limit on the routes
# ---------------------------------------------------------------------------


def test_the_sign_in_routes_answer_429_with_retry_after_beyond_the_limit(world, monkeypatch):
    monkeypatch.setattr(login_rate_limit, "MAX_ATTEMPTS", 3)

    answers = [world.exchange("x").status_code for _ in range(5)]

    assert answers == [401, 401, 401, 429, 429]
    limited = world.exchange("x")
    assert limited.json()["detail"]["error_code"] == rules.ERROR_RATE_LIMITED
    assert 1 <= int(limited.headers["retry-after"]) <= login_rate_limit.WINDOW_SECONDS


def test_the_callback_of_a_limited_client_lands_on_the_error_page(world, monkeypatch):
    state = world.go_out()
    monkeypatch.setattr(login_rate_limit, "MAX_ATTEMPTS", 1)  # the start above was the first and only attempt

    assert world.come_back("x:ext-alice", state) == {"error": rules.ERROR_RATE_LIMITED}


def test_the_limit_counts_per_address_and_its_window_ends():
    clock = [0.0]
    redis = FakeRedis(clock=lambda: clock[0])

    first = [login_rate_limit.hit("10.0.0.1", client=redis, max_attempts=2, window=60).allowed for _ in range(3)]
    other = login_rate_limit.hit("10.0.0.2", client=redis, max_attempts=2, window=60).allowed
    clock[0] = 61.0
    later = login_rate_limit.hit("10.0.0.1", client=redis, max_attempts=2, window=60).allowed

    assert (first, other, later) == ([True, True, False], True, True)


def test_a_counter_left_without_an_expiry_is_given_one():
    redis = FakeRedis()
    redis.set("musizerr:provider-login:10.0.0.1", 5)  # no expiry: it would block the address forever

    verdict = login_rate_limit.hit("10.0.0.1", client=redis, max_attempts=3, window=60)

    assert verdict.allowed is False and redis.ttl("musizerr:provider-login:10.0.0.1") > 0


def test_without_redis_nobody_is_limited():
    class _Down:
        def incr(self, key):
            raise ConnectionError("no redis")

    assert login_rate_limit.hit("10.0.0.1", client=_Down(), max_attempts=1).allowed is True


def test_the_client_address_is_the_one_the_proxy_appended():
    class _Request:
        def __init__(self, forwarded: str | None) -> None:
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
            self.client = type("C", (), {"host": "172.18.0.5"})()

    assert login_rate_limit.client_address(_Request("1.1.1.1, 9.9.9.9")) == "9.9.9.9"  # what a client wrote first is not trusted
    assert login_rate_limit.client_address(_Request(None)) == "172.18.0.5"
