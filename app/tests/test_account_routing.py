"""
The ONE routing rule of the credentials (``db.auth_store.CredentialStore``)
and what ``AuthManager`` does with it: which account a call uses is decided by
the model the provider declares (``musizeer_sdk.auth.AccountModel``), whoever the
caller is. Reads route; writes name their target.
"""
from __future__ import annotations

import time

import pytest
from sqlmodel import Session

from core.auth import AccountTarget
from musizeer_sdk.auth import (
    AccountModel,
    AuthDefinition,
    AuthFlow,
    BearerHeaderRequestAuth,
    ClientCredentialsFallback,
    OAuth2Config,
)
from core.auth_manager import AuthError, AuthManager, ProviderNotConfigured
from db.auth_store import CredentialStore
from db.models import ProviderCredential, User

from tests.conftest import FakeMetadataProvider
from tests.loopback import as_proxy

PERSONAL, SHARED = AccountTarget.PERSONAL, AccountTarget.SHARED


def _oauth_provider(model: AccountModel, *, fallback: bool = False, capability_scopes: dict | None = None):
    provider = FakeMetadataProvider("acct")
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=model,
        oauth2=OAuth2Config(
            authorize_url="https://example.invalid/authorize", token_url="https://example.invalid/token",
            scopes=[], request_auth=BearerHeaderRequestAuth(),
            fallback_auth=ClientCredentialsFallback() if fallback else None,
            capability_scopes=capability_scopes or {},
        ),
    )
    return as_proxy(provider)


def _login_provider(model: AccountModel):
    provider = FakeMetadataProvider("login")
    provider.auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=model)
    return as_proxy(provider)


def _token(access: str, **extra) -> dict:
    return {"access_token": access, "obtained_at": time.time(), "expires_in": 3600, **extra}


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def users(session):
    made = []
    for name in ("alice", "bob"):
        user = User(username=name, password_hash="x", role="user")
        session.add(user)
        made.append(user)
    session.commit()
    return [u.id for u in made]


def _header(store: CredentialStore, provider) -> str | None:
    try:
        return AuthManager(store, token_expiry_buffer=0).authenticate(provider).headers["Authorization"]
    except ProviderNotConfigured:
        return None


# ---------------------------------------------------------------------------
# The rule, by model
# ---------------------------------------------------------------------------


def _app_token(session) -> None:
    """The app-only token the client-credentials fallback serves (cached system-wide)."""
    CredentialStore(session).save_oauth2_app_token("acct", _token("app-only"))


def _connect(session, alice_id, *, own: bool, shared: bool):
    if own:
        CredentialStore(session, user_id=alice_id).save_oauth2_token("acct", _token("own"), PERSONAL)
    if shared:
        CredentialStore(session).save_oauth2_token("acct", _token("shared"), SHARED)


@pytest.mark.parametrize("model, own, shared, expected", [
    (AccountModel.PERSONAL, True, True, "Bearer own"),
    (AccountModel.PERSONAL, False, True, None),
    (AccountModel.PERSONAL_WITH_SHARED, True, True, "Bearer own"),
    (AccountModel.PERSONAL_WITH_SHARED, False, True, "Bearer shared"),
    (AccountModel.PERSONAL_WITH_SHARED, True, False, "Bearer own"),
    (AccountModel.PERSONAL_WITH_SHARED, False, False, None),
    (AccountModel.SYSTEM, True, True, "Bearer shared"),
    (AccountModel.SYSTEM, True, False, None),
])
def test_a_user_call_goes_to_the_account_their_providers_model_says(session, users, model, own, shared, expected):
    alice, _bob = users
    _connect(session, alice, own=own, shared=shared)

    assert _header(CredentialStore(session, user_id=alice), _oauth_provider(model)) == expected


@pytest.mark.parametrize("model, expected", [
    (AccountModel.PERSONAL, None),
    (AccountModel.PERSONAL_WITH_SHARED, "Bearer shared"),
    (AccountModel.SYSTEM, "Bearer shared"),
])
def test_a_job_of_the_host_and_a_guest_only_see_the_shared_account(session, users, model, expected):
    _connect(session, users[0], own=True, shared=True)

    assert _header(CredentialStore(session), _oauth_provider(model)) == expected


def test_another_users_account_is_never_used(session, users):
    alice, bob = users
    _connect(session, alice, own=True, shared=False)

    assert _header(CredentialStore(session, user_id=bob), _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)) is None


@pytest.mark.parametrize("model, expected", [
    (AccountModel.PERSONAL, "Bearer own"),
    (AccountModel.PERSONAL_WITH_SHARED, "Bearer own"),
    (AccountModel.SYSTEM, None),
])
def test_an_action_on_the_users_own_account_never_reaches_the_shared_one(session, users, model, expected):
    alice, _bob = users
    _connect(session, alice, own=True, shared=True)

    assert _header(CredentialStore(session, user_id=alice, own_only=True), _oauth_provider(model)) == expected


def test_an_own_only_call_without_an_own_account_gets_nothing_not_the_shared_one_nor_the_app_token(session, users):
    alice, _bob = users
    _connect(session, alice, own=False, shared=True)
    _app_token(session)
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED, fallback=True)

    with pytest.raises(AuthError):
        AuthManager(CredentialStore(session, user_id=alice, own_only=True), token_expiry_buffer=0).authenticate(provider)
    # The routed call, on the contrary, takes the shared account.
    assert _header(CredentialStore(session, user_id=alice), provider) == "Bearer shared"


def test_the_app_only_token_serves_a_call_without_any_account(session, users):
    alice, _bob = users
    _app_token(session)
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED, fallback=True)

    assert _header(CredentialStore(session, user_id=alice), provider) == "Bearer app-only"


def test_an_own_token_that_cannot_be_used_lets_the_shared_one_serve(session, users):
    alice, _bob = users
    CredentialStore(session, user_id=alice).save_oauth2_token("acct", _token("stale", obtained_at=0), PERSONAL)
    CredentialStore(session).save_oauth2_token("acct", _token("shared"), SHARED)

    assert _header(CredentialStore(session, user_id=alice), _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)) == "Bearer shared"


@pytest.mark.parametrize("model, own, shared, expected", [
    (AccountModel.PERSONAL_WITH_SHARED, True, True, "own-user"),
    (AccountModel.PERSONAL_WITH_SHARED, False, True, "shared-user"),
    (AccountModel.PERSONAL, False, True, None),
    (AccountModel.SYSTEM, True, True, "shared-user"),
])
def test_a_login_account_routes_by_the_same_rule(session, users, model, own, shared, expected):
    alice, _bob = users
    if own:
        CredentialStore(session, user_id=alice).save_credentials("login", {"username": "own-user", "password": "p"}, PERSONAL)
    if shared:
        CredentialStore(session).save_credentials("login", {"username": "shared-user", "password": "p"}, SHARED)
    manager = AuthManager(CredentialStore(session, user_id=alice), token_expiry_buffer=0)
    provider = _login_provider(model)

    if expected is None:
        with pytest.raises(ProviderNotConfigured):
            manager.authenticate(provider)
    else:
        assert manager.authenticate(provider).extra["username"] == expected


# ---------------------------------------------------------------------------
# Writes name their target
# ---------------------------------------------------------------------------


def test_a_write_goes_where_its_target_says_whoever_holds_the_store(session, users):
    alice, _bob = users
    store = CredentialStore(session, user_id=alice)

    store.save_oauth2_token("acct", _token("own"), PERSONAL)
    store.save_oauth2_token("acct", _token("shared"), SHARED)

    assert store.own_oauth2_token("acct").data["access_token"] == "own"
    assert store.shared_oauth2_token("acct").data["access_token"] == "shared"
    assert store.shared_oauth2_token("acct").user_id is None and store.own_oauth2_token("acct").user_id == alice


def test_a_personal_write_needs_a_user(session):
    with pytest.raises(ValueError):
        CredentialStore(session).save_credentials("login", {"username": "u", "password": "p"}, PERSONAL)


def test_disconnecting_one_account_leaves_the_other(session, users):
    alice, _bob = users
    store = CredentialStore(session, user_id=alice)
    _connect(session, alice, own=True, shared=True)

    store.delete_oauth2_token("acct", PERSONAL)

    assert store.own_oauth2_token("acct") is None
    assert store.shared_oauth2_token("acct") is not None


def test_disconnecting_everything_removes_only_the_users_own_accounts(session, users):
    alice, bob = users
    _connect(session, alice, own=True, shared=True)
    CredentialStore(session, user_id=alice).save_credentials("login", {"username": "u", "password": "p"}, PERSONAL)
    CredentialStore(session, user_id=bob).save_oauth2_token("acct", _token("bob"), PERSONAL)

    removed = CredentialStore(session, user_id=alice).delete_all_personal()

    assert removed == 2
    rows = session.query(ProviderCredential).all()
    assert sorted((str(r.user_id), r.credential_type) for r in rows) == sorted([("None", "oauth2_token"), (bob, "oauth2_token")])
    assert CredentialStore(session).delete_all_personal() == 0


def test_a_refreshed_token_replaces_the_row_it_came_from_only_if_it_is_still_there(session, users):
    alice, _bob = users
    store = CredentialStore(session, user_id=alice)
    store.save_oauth2_token("acct", _token("old"), PERSONAL)
    read = store.own_oauth2_token("acct")

    assert store.replace_oauth2_token("acct", read, _token("refreshed")) is True
    assert store.own_oauth2_token("acct").data["access_token"] == "refreshed"
    # The same read, replaced again: the row is no longer what was read.
    assert store.replace_oauth2_token("acct", read, _token("late")) is False
    assert store.own_oauth2_token("acct").data["access_token"] == "refreshed"


def test_a_refresh_never_brings_back_a_disconnected_account(session, users):
    alice, _bob = users
    store = CredentialStore(session, user_id=alice)
    store.save_oauth2_token("acct", _token("old"), PERSONAL)
    read = store.own_oauth2_token("acct")
    store.delete_oauth2_token("acct", PERSONAL)

    assert store.replace_oauth2_token("acct", read, _token("refreshed")) is False
    assert store.own_oauth2_token("acct") is None


def test_a_refreshed_shared_token_stays_shared_and_a_refreshed_own_one_stays_own(session, users, monkeypatch):
    alice, _bob = users
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)
    store = CredentialStore(session, user_id=alice)
    store.save_oauth2_token("acct", _token("stale", obtained_at=0, refresh_token="r"), SHARED)
    manager = AuthManager(store, token_expiry_buffer=0)

    class _Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "fresh", "expires_in": 3600}

    monkeypatch.setattr(manager, "_oauth2_token_request", lambda provider, data: _Reply())

    assert manager.authenticate(provider).headers["Authorization"] == "Bearer fresh"
    assert store.own_oauth2_token("acct") is None
    shared = store.shared_oauth2_token("acct").data
    assert shared["access_token"] == "fresh" and shared["refresh_token"] == "r"


def test_the_code_exchange_stores_the_token_as_the_target_account(session, users, monkeypatch):
    alice, _bob = users
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)
    CredentialStore(session).save_oauth2_client("acct", "id", "secret")

    class _Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "granted", "expires_in": 3600}

    for target in (PERSONAL, SHARED):
        store = CredentialStore(session, user_id=alice) if target == PERSONAL else CredentialStore(session)
        manager = AuthManager(store, token_expiry_buffer=0)
        monkeypatch.setattr(manager, "_oauth2_token_request", lambda provider, data: _Reply())
        manager.complete_oauth2(provider, "code", target)

    reader = CredentialStore(session, user_id=alice)
    assert reader.own_oauth2_token("acct").data["access_token"] == "granted"
    assert reader.shared_oauth2_token("acct").data["access_token"] == "granted"


# ---------------------------------------------------------------------------
# What a provider declares
# ---------------------------------------------------------------------------


def test_a_model_without_login_fits_only_a_flow_without_login():
    AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.SYSTEM)
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.OAUTH2, account_model=AccountModel.NONE)


def test_only_an_oauth_or_login_account_can_be_personal():
    AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL)
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.PERSONAL_WITH_SHARED)
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.EXTERNAL_UTILITY, account_model=AccountModel.PERSONAL)
    AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.SYSTEM)


def test_the_model_is_required_and_must_be_a_model():
    with pytest.raises(TypeError):
        AuthDefinition(flow=AuthFlow.NONE)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AuthDefinition(flow=AuthFlow.NONE, account_model="none")  # type: ignore[arg-type]


def test_every_provider_reaching_the_host_declares_its_model():
    from tests.settings_support import registry_of_every_category

    registry = registry_of_every_category()
    providers = [p for getter in ("all_metadata", "all_download", "all_lyrics", "all_storage", "all_sync", "all_identification")
                 for p in getattr(registry, getter)()]

    assert providers
    assert all(isinstance(p.auth.account_model, AccountModel) for p in providers)


# ---------------------------------------------------------------------------
# What a user "has": the host's questions about accounts
# ---------------------------------------------------------------------------


def test_having_an_own_account_is_never_satisfied_by_the_shared_one_whoever_asks(session, users):
    from services.credentials import has_own_account, has_shared_account, own_store

    alice, _bob = users
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED)
    _connect(session, alice, own=False, shared=True)

    assert has_own_account(own_store(session, alice), provider) is False
    assert has_shared_account(CredentialStore(session), provider) is True

    _connect(session, alice, own=True, shared=False)

    assert has_own_account(own_store(session, alice), provider) is True


def test_a_model_without_personal_accounts_has_no_own_account_even_with_a_row(session, users):
    from services.credentials import has_own_account, own_store

    alice, _bob = users
    CredentialStore(session, user_id=alice).save_oauth2_token("acct", _token("stray"), PERSONAL)

    assert has_own_account(own_store(session, alice), _oauth_provider(AccountModel.SYSTEM)) is False


def test_the_missing_permissions_of_an_own_account_are_the_users_never_the_shared_ones(session, users):
    from services.credentials import own_missing_capabilities, own_store

    alice, _bob = users
    provider = _oauth_provider(AccountModel.PERSONAL_WITH_SHARED, capability_scopes={"cap": ["modify"]})
    CredentialStore(session).save_oauth2_token("acct", _token("shared", scope="read"), SHARED)

    assert own_missing_capabilities(own_store(session, alice), provider) == []

    CredentialStore(session, user_id=alice).save_oauth2_token("acct", _token("own", scope="read"), PERSONAL)

    assert own_missing_capabilities(own_store(session, alice), provider) == ["cap"]
