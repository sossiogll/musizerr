"""
The OAuth2 strategies a provider declares are data (``musizeer_sdk.auth``);
the host implements them (``core.auth_strategies``), by the strategy's type.
These tests pin what each one produces: the token request, the session the
provider's calls use, the fallback session, and the refusal of a strategy the
host does not know.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.auth import OAuth2TokenRequest
from core.auth_manager import AuthError, AuthManager
from core.auth_strategies import (
    UnsupportedAuthStrategy,
    build_fallback_session,
    build_request_session,
    build_token_request,
)
from musizeer_sdk.auth import (
    AccountModel,
    AuthDefinition,
    AuthFlow,
    AuthSession,
    BasicClientAuth,
    BearerHeaderRequestAuth,
    ClientCredentialsFallback,
    OAuth2Config,
    PublicClientFallback,
    QueryParamRequestAuth,
    RequestBodyClientAuth,
)

from musizeer_sdk import wire
from tests.conftest import FakeMetadataProvider
from tests.loopback import as_proxy


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


def test_basic_client_auth_sends_the_client_as_http_basic_and_the_payload_untouched():
    payload = {"grant_type": "authorization_code", "code": "abc"}

    request = build_token_request(BasicClientAuth(), client_id="id", client_secret="secret", payload=payload)

    assert request == OAuth2TokenRequest(data=payload, auth=("id", "secret"))
    assert request.data is not payload


def test_request_body_client_auth_puts_the_client_in_the_form_and_sends_no_basic_auth():
    payload = {"grant_type": "refresh_token", "refresh_token": "r"}

    request = build_token_request(RequestBodyClientAuth(), client_id="id", client_secret="secret", payload=payload)

    assert request == OAuth2TokenRequest(data={**payload, "client_id": "id", "client_secret": "secret"}, auth=None)
    assert payload == {"grant_type": "refresh_token", "refresh_token": "r"}, "the caller's payload is not modified"


# ---------------------------------------------------------------------------
# Calls to the service
# ---------------------------------------------------------------------------


def test_bearer_header_carries_the_access_token():
    session = build_request_session(BearerHeaderRequestAuth(), client_id="id", access_token="tok")

    assert session == AuthSession.oauth2(headers={"Authorization": "Bearer tok"})


@pytest.mark.parametrize("token", [None, ""])
def test_bearer_header_without_a_token_is_refused(token):
    with pytest.raises(ValueError, match="access_token is required"):
        build_request_session(BearerHeaderRequestAuth(), client_id="id", access_token=token)


def test_query_param_carries_what_it_names_and_nothing_else():
    strategy = QueryParamRequestAuth(client_id_param="client_id", access_token_param="access_token")

    assert build_request_session(strategy, client_id="id", access_token="tok") == AuthSession.oauth2(
        params={"client_id": "id", "access_token": "tok"},
    )
    assert build_request_session(QueryParamRequestAuth(client_id_param="key"), client_id="id", access_token="tok") == (
        AuthSession.oauth2(params={"key": "id"})
    )
    assert build_request_session(QueryParamRequestAuth(), client_id="id", access_token="tok") == AuthSession.oauth2(params={})


def test_query_param_omits_a_missing_value_unless_it_is_required():
    optional = QueryParamRequestAuth(client_id_param="client_id", access_token_param="access_token")
    assert build_request_session(optional, client_id=None, access_token=None) == AuthSession.oauth2(params={})

    with pytest.raises(ValueError, match="client_id is required"):
        build_request_session(
            QueryParamRequestAuth(client_id_param="client_id", require_client_id=True), client_id=None, access_token="t",
        )
    with pytest.raises(ValueError, match="access_token is required"):
        build_request_session(
            QueryParamRequestAuth(access_token_param="t", require_access_token=True), client_id="i", access_token=None,
        )


def test_query_param_defaults_are_the_ones_a_provider_relies_on():
    strategy = QueryParamRequestAuth()

    assert (strategy.client_id_param, strategy.access_token_param) == (None, None)
    assert (strategy.require_client_id, strategy.require_access_token) == (False, False)


# ---------------------------------------------------------------------------
# Fallbacks, executed by the AuthManager
# ---------------------------------------------------------------------------


class _Store:
    own_only = False

    def __init__(self, client_id: str | None = "id", app_token: dict | None = None) -> None:
        self._client_id = client_id
        self._app_token = app_token

    def get_oauth2_client_id(self, provider: str) -> str | None:
        return self._client_id

    def get_oauth2_app_token(self, provider: str) -> dict | None:
        return self._app_token


def _provider(request_auth, fallback_auth):
    provider = FakeMetadataProvider("strategies")
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=AccountModel.SYSTEM,
        oauth2=OAuth2Config(
            authorize_url="https://example.invalid/authorize", token_url="https://example.invalid/token",
            scopes=[], request_auth=request_auth, fallback_auth=fallback_auth,
        ),
    )
    return as_proxy(provider)


def test_client_credentials_fallback_builds_the_session_of_the_app_only_token():
    import time

    store = _Store(app_token={"access_token": "app", "obtained_at": time.time(), "expires_in": 3600})
    provider = _provider(BearerHeaderRequestAuth(), ClientCredentialsFallback())

    session = build_fallback_session(ClientCredentialsFallback(), AuthManager(store, token_expiry_buffer=0), provider)

    assert session == AuthSession.oauth2(headers={"Authorization": "Bearer app"})


def test_public_client_fallback_builds_the_session_without_an_access_token():
    store = _Store(client_id="public-id")
    strategy = QueryParamRequestAuth(client_id_param="client_id", access_token_param="access_token")
    provider = _provider(strategy, PublicClientFallback())

    session = build_fallback_session(PublicClientFallback(), AuthManager(store, token_expiry_buffer=0), provider)

    assert session == AuthSession.oauth2(params={"client_id": "public-id"})


# ---------------------------------------------------------------------------
# The set is closed
# ---------------------------------------------------------------------------


@wire.register
@dataclass(frozen=True)
class _ForeignStrategy:
    """A strategy a newer SDK defines (so it crosses the wire): data the host has
    no implementation for."""


class _SubclassedBearer(BearerHeaderRequestAuth):
    """Dispatch is by the exact type: a subclass is not a way to bring code."""


@pytest.mark.parametrize("strategy", [_ForeignStrategy(), _SubclassedBearer(), None, "bearer"])
def test_a_strategy_the_host_does_not_know_is_refused_with_a_clear_error(strategy):
    with pytest.raises(UnsupportedAuthStrategy, match="Unsupported OAuth2 request auth strategy"):
        build_request_session(strategy, client_id="id", access_token="tok")
    with pytest.raises(UnsupportedAuthStrategy, match="Unsupported OAuth2 token client auth strategy"):
        build_token_request(strategy, client_id="id", client_secret="s", payload={})
    with pytest.raises(UnsupportedAuthStrategy, match="Unsupported OAuth2 fallback strategy"):
        build_fallback_session(strategy, AuthManager(_Store(), token_expiry_buffer=0), _provider(None, None))


def test_the_manager_does_not_hide_an_unknown_strategy_as_a_missing_token():
    provider = _provider(_ForeignStrategy(), None)

    with pytest.raises(UnsupportedAuthStrategy):
        AuthManager(_Store(), token_expiry_buffer=0).build_oauth2_session(provider, "tok")


def test_a_value_error_of_a_strategy_is_the_providers_invalid_config():
    provider = _provider(BearerHeaderRequestAuth(), None)

    with pytest.raises(AuthError, match="Invalid OAuth2 request auth config"):
        AuthManager(_Store(), token_expiry_buffer=0).build_oauth2_session(provider, None)
