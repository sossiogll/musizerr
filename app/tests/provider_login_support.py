"""
Fakes for the tests of the sign-in through a provider: providers of nobody's
product, declared the way a real one declares itself (``supports_login`` and
``identify_account``). Shared by ``test_provider_login.py`` and
``test_concurrency.py``.
"""
from __future__ import annotations

from musizeer_sdk.auth import (
    AccountIdentity,
    AccountModel,
    AuthDefinition,
    AuthFlow,
    AuthSession,
    BearerHeaderRequestAuth,
    CredentialField,
    OAuth2Config,
)

from tests.conftest import FakeMetadataProvider


class OAuthLoginProvider(FakeMetadataProvider):
    """An OAuth2 provider whose access token IS the id of the account it was granted to."""

    def __init__(self, name: str, model: AccountModel = AccountModel.PERSONAL_WITH_SHARED, *, login: bool = True) -> None:
        super().__init__(name)
        self.auth = AuthDefinition(
            flow=AuthFlow.OAUTH2, account_model=model,
            oauth2=OAuth2Config(
                authorize_url=f"https://{name}.test/authorize", token_url=f"https://{name}.test/token",
                scopes=["profile"], request_auth=BearerHeaderRequestAuth(),
            ),
        )
        self._login = login
        self.identify_calls = 0
        self.unreachable = False

    def supports_login(self) -> bool:
        return self._login

    def identify_account(self, session: AuthSession) -> AccountIdentity | None:
        self.identify_calls += 1
        if self.unreachable:
            raise ConnectionError("provider unreachable")
        token = session.headers["Authorization"].removeprefix("Bearer ")
        if token == "unknown":
            return None
        display, _, ext = token.partition(":")
        return AccountIdentity(external_id=ext or display, display_name=display if ext else None)


class FormLoginProvider(FakeMetadataProvider):
    """A provider with a login account (``username`` + ``password``): the password ``good`` is the only one it accepts."""

    def __init__(self, name: str, model: AccountModel = AccountModel.PERSONAL, *, login: bool = True) -> None:
        super().__init__(name)
        self.auth = AuthDefinition(
            flow=AuthFlow.CREDENTIALS, account_model=model,
            credential_fields=(CredentialField("username", "Username"), CredentialField("password", "Password", secret=True)),
        )
        self._login = login

    def supports_login(self) -> bool:
        return self._login

    def identify_account(self, session: AuthSession) -> AccountIdentity | None:
        username = session.extra["username"]
        if username == "down":
            raise ConnectionError("provider unreachable")
        if session.extra["password"] != "good":
            return None
        return AccountIdentity(external_id=f"id-{username}", display_name=username)
