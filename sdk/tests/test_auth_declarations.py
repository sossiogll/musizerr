"""
The OAuth2 strategies are data: frozen dataclasses that carry values and no
behaviour. What each one does is the host's business, chosen by its type.
"""
from __future__ import annotations

import dataclasses

import pytest

from musizeer_sdk import auth
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

STRATEGIES = [
    BasicClientAuth,
    RequestBodyClientAuth,
    BearerHeaderRequestAuth,
    QueryParamRequestAuth,
    ClientCredentialsFallback,
    PublicClientFallback,
]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_strategy_is_a_frozen_dataclass_with_no_behaviour(strategy):
    assert dataclasses.is_dataclass(strategy)
    assert strategy.__dataclass_params__.frozen
    behaviour = [
        name for name, value in vars(strategy).items()
        if callable(value) and not (name.startswith("__") and name.endswith("__"))
    ]
    assert behaviour == []
    # No base class that brings behaviour either.
    assert strategy.__mro__ == (strategy, object)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_strategy_is_immutable_hashable_and_compares_by_value(strategy):
    one, other = strategy(), strategy()

    assert one == other and hash(one) == hash(other)
    with pytest.raises(dataclasses.FrozenInstanceError):
        one.anything = 1  # type: ignore[attr-defined]


def test_the_query_param_style_keeps_its_fields_and_defaults():
    names = [(f.name, f.default) for f in dataclasses.fields(QueryParamRequestAuth)]

    assert names == [
        ("client_id_param", None),
        ("access_token_param", None),
        ("require_client_id", False),
        ("require_access_token", False),
    ]


def test_the_strategy_kinds_are_the_closed_set_of_the_oauth2_config():
    config = OAuth2Config(
        authorize_url="https://a", token_url="https://t", scopes=["s"],
        token_client_auth=BasicClientAuth(), request_auth=BearerHeaderRequestAuth(), fallback_auth=PublicClientFallback(),
    )

    assert (config.token_client_auth, config.request_auth, config.fallback_auth) == (
        BasicClientAuth(), BearerHeaderRequestAuth(), PublicClientFallback(),
    )
    assert auth.TokenClientAuth == BasicClientAuth | RequestBodyClientAuth
    assert auth.RequestAuth == BearerHeaderRequestAuth | QueryParamRequestAuth
    assert auth.FallbackAuth == ClientCredentialsFallback | PublicClientFallback


def test_requested_scopes_are_the_scopes_plus_each_capability_scope_once():
    config = OAuth2Config(
        authorize_url="https://a", token_url="https://t", scopes=["a", "b"],
        capability_scopes={"x": ["b", "c"], "y": ["c", "d"]},
    )

    assert config.requested_scopes() == ["a", "b", "c", "d"]


def test_an_account_model_must_fit_the_flow():
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.SYSTEM)
    with pytest.raises(ValueError):
        AuthDefinition(flow=AuthFlow.API_KEY, account_model=AccountModel.PERSONAL)
    with pytest.raises(TypeError):
        AuthDefinition(flow=AuthFlow.NONE, account_model="none")  # type: ignore[arg-type]
    assert AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE).account_model is AccountModel.NONE


def test_a_session_offers_the_headers_and_params_as_copies():
    session = AuthSession.oauth2(headers={"h": "1"}, params={"p": "2"})

    assert session.auth_headers() == {"h": "1"} and session.auth_headers() is not session.headers
    assert session.auth_params() == {"p": "2"} and session.auth_params() is not session.params


def test_an_external_utility_names_the_paths_of_its_intake_only_with_a_safe_name():
    assert auth.ExternalUtilityConfig(credential_type="sessions").intake_name is None
    assert auth.ExternalUtilityConfig(credential_type="sessions", intake_name="my-service2").intake_name == "my-service2"
    for unsafe in ("", "Upper", "a/b", "..", "a b", "-x", "1x", "x" * 65, "a_api_config"):
        with pytest.raises(ValueError):
            auth.ExternalUtilityConfig(credential_type="sessions", intake_name=unsafe)
