"""
The host's implementation of the declarative OAuth2 strategies
(``musizerr_sdk.auth``). A provider declares WHICH strategy it uses, as data;
what each one does lives here, selected by the strategy's type. The set is
closed: a strategy this module does not know is refused with a clear error
instead of being run, and a new one is a change of the SDK and of this module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from musizerr_sdk.auth import (
    AuthSession,
    BasicClientAuth,
    BearerHeaderRequestAuth,
    ClientCredentialsFallback,
    PublicClientFallback,
    QueryParamRequestAuth,
    RequestBodyClientAuth,
)

from .auth import OAuth2TokenRequest

if TYPE_CHECKING:
    from musizerr_sdk.interfaces import BaseProvider

    from .auth_manager import AuthManager


class UnsupportedAuthStrategy(TypeError):
    """A provider declared an OAuth2 strategy the host does not implement."""


def _lookup(table: dict[type, Callable[..., Any]], strategy: object, kind: str) -> Callable[..., Any]:
    implementation = table.get(type(strategy))
    if implementation is None:
        known = ", ".join(sorted(t.__name__ for t in table))
        raise UnsupportedAuthStrategy(
            f"Unsupported OAuth2 {kind} strategy {type(strategy).__name__!r}; the host implements: {known}"
        )
    return implementation


# ------------------------------------------------------------------
# How the client authenticates at the token endpoint
# ------------------------------------------------------------------

def _basic_client_auth(
    strategy: BasicClientAuth, *, client_id: str, client_secret: str, payload: dict[str, Any],
) -> OAuth2TokenRequest:
    return OAuth2TokenRequest(data=dict(payload), auth=(client_id, client_secret))


def _request_body_client_auth(
    strategy: RequestBodyClientAuth, *, client_id: str, client_secret: str, payload: dict[str, Any],
) -> OAuth2TokenRequest:
    data = dict(payload)
    data["client_id"] = client_id
    data["client_secret"] = client_secret
    return OAuth2TokenRequest(data=data)


_TOKEN_CLIENT_AUTH: dict[type, Callable[..., OAuth2TokenRequest]] = {
    BasicClientAuth: _basic_client_auth,
    RequestBodyClientAuth: _request_body_client_auth,
}


def build_token_request(
    strategy: object, *, client_id: str, client_secret: str, payload: dict[str, Any],
) -> OAuth2TokenRequest:
    """The HTTP payload/auth tuple for the token endpoint."""
    return _lookup(_TOKEN_CLIENT_AUTH, strategy, "token client auth")(
        strategy, client_id=client_id, client_secret=client_secret, payload=payload,
    )


# ------------------------------------------------------------------
# How calls to the service carry the token
# ------------------------------------------------------------------

def _bearer_header(
    strategy: BearerHeaderRequestAuth, *, client_id: str | None, access_token: str | None,
) -> AuthSession:
    if not access_token:
        raise ValueError("access_token is required for bearer-authenticated requests")
    return AuthSession.oauth2(headers={"Authorization": f"Bearer {access_token}"})


def _query_param(
    strategy: QueryParamRequestAuth, *, client_id: str | None, access_token: str | None,
) -> AuthSession:
    params: dict[str, str] = {}

    if strategy.client_id_param:
        if client_id:
            params[strategy.client_id_param] = client_id
        elif strategy.require_client_id:
            raise ValueError("client_id is required for this request style")

    if strategy.access_token_param:
        if access_token:
            params[strategy.access_token_param] = access_token
        elif strategy.require_access_token:
            raise ValueError("access_token is required for this request style")

    return AuthSession.oauth2(params=params)


_REQUEST_AUTH: dict[type, Callable[..., AuthSession]] = {
    BearerHeaderRequestAuth: _bearer_header,
    QueryParamRequestAuth: _query_param,
}


def build_request_session(strategy: object, *, client_id: str | None, access_token: str | None) -> AuthSession:
    """The ``AuthSession`` the provider's HTTP calls use. Raises ``ValueError``
    when the strategy needs a value that is missing."""
    return _lookup(_REQUEST_AUTH, strategy, "request auth")(strategy, client_id=client_id, access_token=access_token)


# ------------------------------------------------------------------
# What to use when no valid token is stored
# ------------------------------------------------------------------

def _client_credentials(
    strategy: ClientCredentialsFallback, auth_manager: AuthManager, provider: BaseProvider,
) -> AuthSession | None:
    token_data = auth_manager.fetch_client_credentials_token(provider)
    return auth_manager.build_oauth2_session(provider, token_data["access_token"])


def _public_client(
    strategy: PublicClientFallback, auth_manager: AuthManager, provider: BaseProvider,
) -> AuthSession | None:
    return auth_manager.build_oauth2_session(provider, access_token=None)


_FALLBACK: dict[type, Callable[..., AuthSession | None]] = {
    ClientCredentialsFallback: _client_credentials,
    PublicClientFallback: _public_client,
}


def build_fallback_session(strategy: object, auth_manager: AuthManager, provider: BaseProvider) -> AuthSession | None:
    """A fallback session when no valid token is stored."""
    return _lookup(_FALLBACK, strategy, "fallback")(strategy, auth_manager, provider)
