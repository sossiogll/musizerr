"""
The declarative auth types: what a provider says about how one signs in to its
service, and the ``AuthSession`` it receives on every call. The provider only
DECLARES; the host executes the flows (token requests, refreshes, storage).

The OAuth2 strategies are data: a closed set of frozen dataclasses naming a
behaviour the host implements. A provider cannot bring code for the host to
run; a new strategy is a change of this package.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class AuthFlow(Enum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    CREDENTIALS = "credentials"
    EXTERNAL_UTILITY = "external_utility"


class AccountModel(str, Enum):
    """Whose account a provider is used with, declared by the provider (never
    inferred from its category or its flow). The host routes every credential
    by it: one rule for everybody.

    - ``NONE``: no account at all (a public catalog, a lyrics service, a local
      folder). Users see nothing; the instance may still have settings.
    - ``PERSONAL``: only each user's own account, never a shared one.
    - ``PERSONAL_WITH_SHARED``: users may connect their own account; the
      instance also has a shared one (managed by an admin), used for the users
      who have none and by the host's jobs.
    - ``SYSTEM``: only the instance's account, managed by an admin.
    """

    NONE = "none"
    PERSONAL = "personal"
    PERSONAL_WITH_SHARED = "personal_with_shared"
    SYSTEM = "system"

    @property
    def has_personal(self) -> bool:
        """Users can connect their own account."""
        return self in (AccountModel.PERSONAL, AccountModel.PERSONAL_WITH_SHARED)

    @property
    def has_shared(self) -> bool:
        """The instance has an account of its own, used by the host."""
        return self in (AccountModel.PERSONAL_WITH_SHARED, AccountModel.SYSTEM)


@dataclass(frozen=True)
class CredentialField:
    """One field of the form a user fills to connect an account made of
    credentials. A ``secret`` one is never shown back."""

    key: str
    label: str
    secret: bool = False


@dataclass(frozen=True)
class AccountIdentity:
    """Who an account is on its provider, as the provider says it: a stable id
    (never an email or a name that can change hands) and, for display only, a
    name. What a sign-in through a linked account is matched by
    (``BaseProvider.identify_account``)."""

    external_id: str
    display_name: str | None = None


USERNAME_PASSWORD_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("username", "Username"),
    CredentialField("password", "Password", secret=True),
)


# ------------------------------------------------------------------
# OAuth2 strategies: data only, implemented by the host
# ------------------------------------------------------------------

@dataclass(frozen=True)
class BasicClientAuth:
    """Token endpoint: the client authenticates with HTTP Basic
    (``client_id``:``client_secret``)."""


@dataclass(frozen=True)
class RequestBodyClientAuth:
    """Token endpoint: ``client_id`` and ``client_secret`` go in the form body."""


@dataclass(frozen=True)
class BearerHeaderRequestAuth:
    """Calls to the service carry ``Authorization: Bearer <access token>``."""


@dataclass(frozen=True)
class QueryParamRequestAuth:
    """Calls to the service carry the client id and/or the access token as
    query parameters, named here (a parameter without a name is not sent).
    ``require_*`` makes a missing value an error instead of an omission."""

    client_id_param: str | None = None
    access_token_param: str | None = None
    require_client_id: bool = False
    require_access_token: bool = False


@dataclass(frozen=True)
class ClientCredentialsFallback:
    """With no stored user token, calls use an app-only token obtained with the
    client-credentials grant."""


@dataclass(frozen=True)
class PublicClientFallback:
    """With no stored user token, calls are made as the public client (the
    request style without an access token)."""


TokenClientAuth = BasicClientAuth | RequestBodyClientAuth
RequestAuth = BearerHeaderRequestAuth | QueryParamRequestAuth
FallbackAuth = ClientCredentialsFallback | PublicClientFallback


# ------------------------------------------------------------------
# Configurazione auth
# ------------------------------------------------------------------

@dataclass
class OAuth2Config:
    authorize_url: str
    token_url: str
    scopes: list[str]
    redirect_uri: str = ""
    token_client_auth: TokenClientAuth | None = None
    request_auth: RequestAuth | None = None
    fallback_auth: FallbackAuth | None = None
    # Scopes an optional capability needs, by capability key (e.g.
    # ``{"unlike_saved_track": ["library-modify"]}``, keys as
    # ``musizerr_sdk.capabilities.CAPABILITY_UNLIKE_SAVED_TRACK``). Requested with ``scopes`` on
    # every new connection; a connection granted before a scope was added lacks
    # it, and the host tells the user to reconnect (``missing_capabilities``).
    capability_scopes: dict[str, list[str]] = field(default_factory=dict)
    # Extra query parameters of the authorize URL, for a service that asks for
    # more than the standard ones (e.g. to hand out a refresh token). They
    # never replace a parameter the host sets itself (client id, redirect URI,
    # scope, state, response type).
    extra_authorize_params: dict[str, str] = field(default_factory=dict)

    def requested_scopes(self) -> list[str]:
        """``scopes`` plus every capability scope, each once, in order."""
        return list(dict.fromkeys([*self.scopes, *(s for scopes in self.capability_scopes.values() for s in scopes)]))


@dataclass
class ApiKeyConfig:
    header_name: str | None = None
    query_param: str | None = None


@dataclass(frozen=True)
class AccountDisplayField:
    key: str
    label: str


@dataclass
class ExternalUtilityConfig:
    """
    Auth completed by an external utility that POSTs credentials to Musizerr.
    The utility drives the flow; the provider is 'authenticated' as soon as at
    least one account is registered.

    The utility is a program of its own with a fixed API toward the host, so the
    provider declares the name that API uses (``intake_name``) and the host
    serves it. With ``intake_name`` N the utility calls, with an admin's
    credentials, ``GET /credentials/N`` (the accounts the host holds, shown with
    ``account_display_fields``), ``POST|DELETE /credentials/N/{account}`` (register
    one: any JSON object, kept as received and handed to the provider in its
    ``AuthSession``, or remove it) and, when the provider borrows another
    provider's OAuth client (``borrow_oauth_client_from``),
    ``GET|PUT /credentials/N_api_config`` (whether that client is set up, and set
    it up; the secret is never returned). Without ``intake_name`` the host serves
    nothing for it.
    """
    credential_type: str
    borrow_oauth_client_from: str | None = None
    library_package: str | None = None
    library_url: str | None = None
    disclaimer: str | None = None
    setup_command: str | None = None
    setup_instructions: str | None = None
    account_display_fields: list[AccountDisplayField] = field(default_factory=list)
    intake_name: str | None = None

    def __post_init__(self) -> None:
        # It goes into a URL path: lowercase letters, digits and hyphens, nothing that could add a segment.
        if self.intake_name is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.intake_name):
            raise ValueError(f"intake_name must be lowercase letters, digits and hyphens, got {self.intake_name!r}")


@dataclass
class AuthDefinition:
    flow: AuthFlow
    # Whose account the provider is used with. Required: a provider states it,
    # the host never guesses it from the flow or the category.
    account_model: AccountModel
    oauth2: OAuth2Config | None = None
    api_key: ApiKeyConfig | None = None
    external_utility: ExternalUtilityConfig | None = None
    # The form of a ``CREDENTIALS`` account (what the user fills to connect).
    credential_fields: tuple[CredentialField, ...] = USERNAME_PASSWORD_FIELDS

    def __post_init__(self) -> None:
        if not isinstance(self.account_model, AccountModel):
            raise TypeError(f"account_model must be an AccountModel, got {self.account_model!r}")
        # A flow without login has no account; every other flow has one. A
        # personal account exists only for the flows a user can walk through.
        if (self.flow == AuthFlow.NONE) != (self.account_model == AccountModel.NONE):
            raise ValueError(f"account model '{self.account_model.value}' does not fit the flow '{self.flow.value}'")
        if self.account_model.has_personal and self.flow not in (AuthFlow.OAUTH2, AuthFlow.CREDENTIALS):
            raise ValueError(f"a '{self.flow.value}' account cannot be personal")


@dataclass
class AuthSession:
    """Risultato dell'autenticazione, passato al provider per fare le chiamate."""
    flow: AuthFlow
    headers: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def anonymous(cls) -> AuthSession:
        return cls(flow=AuthFlow.NONE)

    @classmethod
    def oauth2(
        cls,
        *,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> AuthSession:
        return cls(
            flow=AuthFlow.OAUTH2,
            headers=headers or {},
            params=params or {},
        )

    @classmethod
    def external_utility(
        cls,
        *,
        accounts: dict,
        borrowed_oauth_client: dict | None = None,
    ) -> AuthSession:
        return cls(
            flow=AuthFlow.EXTERNAL_UTILITY,
            extra={
                "accounts": accounts,
                "borrowed_oauth_client": borrowed_oauth_client or {},
            },
        )

    @classmethod
    def with_api_key(
        cls,
        config: ApiKeyConfig,
        key: str,
    ) -> AuthSession:
        headers = {}
        params = {}
        if config.header_name:
            headers[config.header_name] = key
        if config.query_param:
            params[config.query_param] = key
        return cls(
            flow=AuthFlow.API_KEY,
            headers=headers,
            params=params,
        )

    def auth_headers(self) -> dict:
        return dict(self.headers)

    def auth_params(self) -> dict:
        return dict(self.params)
