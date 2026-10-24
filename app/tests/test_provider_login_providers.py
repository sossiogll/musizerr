"""
What a provider that declares the sign-in (``supports_login`` and
``identify_account``) gives the host through the call protocol, and what one
that does not declare it keeps:

- the account is identified by the id the provider answers (the host never
  asks for an email), an account the service does not recognise is ``None``;
- the service refusing for now is a failure of the service
  (``ProviderRateLimited``), not an unknown account;
- the capability the host names from the declaration.

How a real provider asks its service who an account is, is tested in its own
repository.
"""
from __future__ import annotations

import pytest

from core.interfaces import personal_capabilities
from musizeer_sdk.auth import AccountIdentity, AuthSession
from musizeer_sdk.capabilities import CAPABILITY_LOGIN
from musizeer_sdk.errors import ProviderRateLimited

from tests.loopback import as_proxy
from tests.provider_login_support import FormLoginProvider, OAuthLoginProvider

TOKEN = AuthSession.oauth2(headers={"Authorization": "Bearer Alice:u-1"})


def test_the_account_is_identified_by_the_id_the_provider_answers():
    provider = as_proxy(OAuthLoginProvider("one"))

    assert provider.supports_login() is True
    assert provider.identify_account(TOKEN) == AccountIdentity("u-1", "Alice")
    assert personal_capabilities(provider)[0] == CAPABILITY_LOGIN


def test_a_token_the_service_does_not_recognise_is_no_account():
    assert as_proxy(OAuthLoginProvider("one")).identify_account(
        AuthSession.oauth2(headers={"Authorization": "Bearer unknown"})) is None


def test_a_service_that_refuses_for_now_is_a_failure_not_an_unknown_account():
    class _Refusing(OAuthLoginProvider):
        def identify_account(self, session):
            raise ProviderRateLimited("quota exhausted", retry_after=30)

    with pytest.raises(ProviderRateLimited) as raised:
        as_proxy(_Refusing("one")).identify_account(TOKEN)

    assert raised.value.retry_after == 30


def test_the_credentials_of_a_form_are_checked_by_the_provider():
    provider = as_proxy(FormLoginProvider("form"))
    good = AuthSession(flow=provider.auth.flow, extra={"username": "alice", "password": "good"})
    bad = AuthSession(flow=provider.auth.flow, extra={"username": "alice", "password": "bad"})

    assert provider.identify_account(good) == AccountIdentity("id-alice", "alice")
    assert provider.identify_account(bad) is None


def test_a_provider_that_does_not_declare_it_keeps_the_defaults():
    for provider in (OAuthLoginProvider("one", login=False), FormLoginProvider("form", login=False)):
        proxy = as_proxy(provider)
        assert proxy.supports_login() is False
        assert CAPABILITY_LOGIN not in personal_capabilities(proxy)
