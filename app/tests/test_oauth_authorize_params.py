"""
``OAuth2Config.extra_authorize_params``: a provider whose service asks for more
than the standard query parameters on the authorize URL (e.g. offline access to
be given a refresh token) declares them; ``AuthManager`` adds them, and they
never replace what it sets itself.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, OAuth2Config
from core.auth_manager import AuthManager
from musizeer_sdk.auth import BearerHeaderRequestAuth

from tests.conftest import FakeMetadataProvider
from tests.loopback import as_proxy


class _Store:
    def get_oauth2_client(self, name):
        return {"client_id": "cid", "client_secret": "secret"}


def _provider(**oauth2_kwargs) -> FakeMetadataProvider:
    provider = FakeMetadataProvider("oauthy")
    provider.auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=AccountModel.PERSONAL_WITH_SHARED,
        oauth2=OAuth2Config(
            authorize_url="https://idp.example/authorize",
            token_url="https://idp.example/token",
            scopes=["read"],
            request_auth=BearerHeaderRequestAuth(),
            **oauth2_kwargs,
        ),
    )
    return provider


def _query(provider, state="st") -> dict[str, list[str]]:
    url = AuthManager(_Store(), token_expiry_buffer=60).get_authorize_url(as_proxy(provider), state=state)
    assert url.startswith("https://idp.example/authorize?")
    return parse_qs(urlparse(url).query)


def test_without_extra_params_the_url_is_the_standard_one():
    query = _query(_provider())

    assert set(query) == {"client_id", "response_type", "redirect_uri", "scope", "state"}
    assert OAuth2Config("a", "t", []).extra_authorize_params == {}


def test_extra_params_are_added_to_the_authorize_url():
    query = _query(_provider(extra_authorize_params={"access_type": "offline", "prompt": "consent"}))

    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["client_id"] == ["cid"] and query["scope"] == ["read"] and query["state"] == ["st"]


def test_extra_params_never_replace_the_ones_the_manager_sets():
    query = _query(_provider(extra_authorize_params={
        "client_id": "other", "state": "forged", "scope": "admin", "response_type": "token", "redirect_uri": "http://x",
    }))

    assert query["client_id"] == ["cid"]
    assert query["state"] == ["st"]
    assert query["scope"] == ["read"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] != ["http://x"]


def test_the_extra_params_of_one_config_are_not_shared_with_another():
    first, second = OAuth2Config("a", "t", []), OAuth2Config("a", "t", [])
    first.extra_authorize_params["x"] = "1"

    assert second.extra_authorize_params == {}
