from __future__ import annotations
import os
import time
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlencode

import httpx

from .auth import AccountTarget, StoredCredential
from .auth_strategies import build_fallback_session, build_request_session, build_token_request
from musizerr_sdk.auth import AuthFlow, AuthSession

if TYPE_CHECKING:
    from musizerr_sdk.interfaces import BaseProvider

_APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")


class AuthError(Exception):
    pass


class ProviderNotConfigured(AuthError):
    """Il provider non ha le credenziali che il suo flusso richiede (nessuna
    API key, nessun account collegato): non è un guasto, è una scelta di
    configurazione. Chi aggrega più provider lo salta in silenzio."""


class AuthManager:
    """
    Esegue i flussi di autenticazione dichiarati dai provider.
    I provider dichiarano il flusso, AuthManager lo esegue.
    Le credenziali sono lette e scritte tramite il credential_store.
    """

    def __init__(self, credential_store, *, token_expiry_buffer: int) -> None:
        # credential_store: istanza di db.auth_store.CredentialStore.
        # token_expiry_buffer: secondi prima della scadenza in cui un token
        # OAuth2 va considerato già scaduto e rinnovato (politica dell'host).
        self._store = credential_store
        self._token_expiry_buffer = token_expiry_buffer

    def authenticate(self, provider: BaseProvider) -> AuthSession:
        """The session of the account the store routes ``provider`` to (see
        ``db.auth_store.CredentialStore``). A store that acts on the user's
        own account only (``own_only``) never gets the provider's fallback
        (an app-only token) either."""
        match provider.auth.flow:
            case AuthFlow.NONE:
                return AuthSession.anonymous()
            case AuthFlow.API_KEY:
                return self._handle_api_key(provider)
            case AuthFlow.OAUTH2:
                return self._handle_oauth2(provider)
            case AuthFlow.CREDENTIALS:
                return self._handle_credentials(provider)
            case AuthFlow.EXTERNAL_UTILITY:
                return self._handle_external_utility(provider)
            case _:
                raise AuthError(f"Unknown auth flow: {provider.auth.flow}")

    # ------------------------------------------------------------------
    # API KEY
    # ------------------------------------------------------------------

    def _handle_api_key(self, provider: BaseProvider) -> AuthSession:
        key = self._store.get_api_key(provider.name)
        if not key:
            raise ProviderNotConfigured(f"No API key configured for provider '{provider.name}'")
        session = AuthSession.with_api_key(provider.auth.api_key, key)
        config = self._provider_config(provider)
        if config:
            session.extra.update(config)
        return session

    @staticmethod
    def _provider_config(provider: BaseProvider) -> dict:
        """Config operativa (non segreta) del provider, dal suo contesto:
        questa classe gestisce solo i segreti del credential store."""
        return dict(provider.context.settings())

    # ------------------------------------------------------------------
    # OAUTH2
    # ------------------------------------------------------------------

    def _handle_oauth2(self, provider: BaseProvider) -> AuthSession:
        cfg = self._oauth2_config(provider)

        # The accounts the store routes this provider to, own first. One that
        # cannot be used (expired, refresh refused) lets the next one serve.
        for stored in self._store.oauth2_token_candidates(provider.name, provider.auth.account_model):
            session = self._session_from_token(provider, stored)
            if session is not None:
                return session

        if cfg.fallback_auth and not self._store.own_only:
            try:
                session = build_fallback_session(cfg.fallback_auth, self, provider)
                if session is not None:
                    return session
            except AuthError:
                pass

        raise ProviderNotConfigured(
            f"Provider '{provider.name}' requires OAuth2 authorization. "
            f"Start the flow at /auth/{provider.name}/start"
        )

    def _session_from_token(self, provider: BaseProvider, stored: StoredCredential) -> AuthSession | None:
        """The session of a stored token, refreshed if it expired; None when it can be neither used nor refreshed."""
        token = stored.data
        if token.get("access_token") and not self._is_expired(token):
            return self.build_oauth2_session(provider, token["access_token"])
        if token.get("refresh_token"):
            try:
                return self._refresh_oauth2(provider, stored)
            except AuthError:
                return None
        return None

    def _refresh_oauth2(self, provider: BaseProvider, stored: StoredCredential) -> AuthSession:
        token_data = self._request_oauth2_token(
            provider,
            {"grant_type": "refresh_token", "refresh_token": stored.data["refresh_token"]},
            error_context="Token refresh failed",
        )
        # Refresh responses often omit refresh_token (the provider expects the
        # caller to keep reusing the original one) — losing it here would
        # permanently strand the account until the user re-authorizes from scratch.
        token_data.setdefault("refresh_token", stored.data["refresh_token"])
        # Written back over the very row it came from, and only if that row is
        # still as read: a disconnected or reconnected account is left alone.
        self._store.replace_oauth2_token(provider.name, stored, token_data)
        return self.build_oauth2_session(provider, token_data["access_token"])

    def exchange_oauth2_code(self, provider: BaseProvider, code: str, target: AccountTarget) -> dict:
        """The token the authorization ``code`` is worth, NOT stored. A service
        that hands a refresh token out only on the first consent keeps the one
        of the ``target`` account already stored."""
        token_data = self._request_oauth2_token(
            provider,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri(provider),
            },
            error_context="OAuth2 code exchange failed",
        )
        if "refresh_token" not in token_data:
            previous = (
                self._store.shared_oauth2_token(provider.name)
                if target == AccountTarget.SHARED
                else self._store.own_oauth2_token(provider.name)
            )
            if previous and previous.data.get("refresh_token"):
                token_data["refresh_token"] = previous.data["refresh_token"]
        return token_data

    def complete_oauth2(
        self,
        provider: BaseProvider,
        code: str,
        target: AccountTarget,
        *,
        account_id: Callable[[AuthSession], str | None] | None = None,
    ) -> AuthSession:
        """Chiamato dal callback endpoint dopo che l'utente ha autorizzato:
        the token is stored as the ``target`` account. ``account_id``, given
        for a personal account of a provider that can sign users in, is asked
        who the account is (it receives the session of the new token and may
        raise): the id is stored with the account, and an account whose id
        cannot be told is not stored at all."""
        token_data = self.exchange_oauth2_code(provider, code, target)
        session = self.build_oauth2_session(provider, token_data["access_token"])
        external_id = account_id(session) if account_id is not None and target == AccountTarget.PERSONAL else None
        self._store.save_oauth2_token(provider.name, token_data, target, external_id=external_id)
        return session

    def get_authorize_url(self, provider: BaseProvider, state: str = "") -> str:
        cfg = self._oauth2_config(provider)
        params = {
            **cfg.extra_authorize_params,
            "client_id": self.require_oauth2_client(provider)["client_id"],
            "response_type": "code",
            "redirect_uri": self._redirect_uri(provider),
            "scope": " ".join(cfg.requested_scopes()),
            "state": state,
        }
        query = urlencode({k: v for k, v in params.items() if v})
        return f"{cfg.authorize_url}?{query}"

    def require_oauth2_client(self, provider: BaseProvider) -> dict:
        client = self._store.get_oauth2_client(provider.name)
        if not client or not client.get("client_id") or not client.get("client_secret"):
            raise AuthError(f"No OAuth2 client configured for provider '{provider.name}'")
        return client

    def _request_oauth2_token(self, provider: BaseProvider, data: dict, *, error_context: str) -> dict:
        resp = self._oauth2_token_request(provider, data)
        if resp.status_code != 200:
            raise AuthError(f"{error_context} for '{provider.name}': {resp.text}")
        token_data = resp.json()
        token_data["obtained_at"] = time.time()
        return token_data

    def fetch_client_credentials_token(self, provider: BaseProvider) -> dict:
        """App-only token (no user context) — always cached system-wide, in its
        own slot, so it never collides with or overwrites a real user OAuth2 token."""
        cached = self._store.get_oauth2_app_token(provider.name)
        if cached and not self._is_expired(cached):
            return cached
        resp = self._oauth2_token_request(provider, {"grant_type": "client_credentials"})
        if resp.status_code != 200:
            raise AuthError(f"Client credentials refresh failed for '{provider.name}': {resp.text}")
        token_data = resp.json()
        token_data["obtained_at"] = time.time()
        self._store.save_oauth2_app_token(provider.name, token_data)
        return token_data

    def build_oauth2_session(self, provider: BaseProvider, access_token: str | None) -> AuthSession:
        cfg = self._oauth2_config(provider)
        if not cfg.request_auth:
            raise AuthError(f"Provider '{provider.name}' is missing an OAuth2 request auth strategy")

        try:
            session = build_request_session(
                cfg.request_auth,
                client_id=self._store.get_oauth2_client_id(provider.name),
                access_token=access_token,
            )
        except ValueError as exc:
            raise AuthError(f"Invalid OAuth2 request auth config for '{provider.name}': {exc}") from exc

        config = self._provider_config(provider)
        if config:
            session.extra.update(config)
        return session

    def _oauth2_token_request(self, provider: BaseProvider, data: dict) -> httpx.Response:
        cfg = self._oauth2_config(provider)
        if not cfg.token_client_auth:
            raise AuthError(f"Provider '{provider.name}' is missing an OAuth2 token auth strategy")

        client = self.require_oauth2_client(provider)
        request = build_token_request(
            cfg.token_client_auth,
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            payload=data,
        )

        return httpx.post(
            cfg.token_url,
            data=request.data,
            auth=request.auth,
            timeout=provider.context.http_timeout,
        )

    def _redirect_uri(self, provider: BaseProvider) -> str:
        uri = self._oauth2_config(provider).redirect_uri
        if not uri:
            uri = f"{_APP_BASE_URL}/auth/{provider.name}/callback"
        return uri

    # ------------------------------------------------------------------
    # CREDENTIALS (username + password, raro)
    # ------------------------------------------------------------------

    def _handle_credentials(self, provider: BaseProvider) -> AuthSession:
        accounts = self._store.credentials_candidates(provider.name, provider.auth.account_model)
        if not accounts:
            raise ProviderNotConfigured(f"No credentials configured for provider '{provider.name}'")
        return AuthSession(flow=AuthFlow.CREDENTIALS, extra=accounts[0].data)

    # ------------------------------------------------------------------
    # EXTERNAL UTILITY (credenziali iniettate da un tool esterno)
    # ------------------------------------------------------------------

    def _handle_external_utility(self, provider: BaseProvider) -> AuthSession:
        cfg = provider.auth.external_utility
        if not cfg:
            raise AuthError(f"Provider '{provider.name}' is missing external_utility configuration")

        accounts = self._store.get_external_utility_accounts(provider.name, cfg.credential_type)
        if not accounts:
            raise ProviderNotConfigured(
                f"Provider '{provider.name}' has no accounts registered yet. "
                f"Run the external utility to register one."
            )

        borrowed: dict | None = None
        if cfg.borrow_oauth_client_from:
            borrowed = self._store.get_oauth2_client(cfg.borrow_oauth_client_from)
            if not borrowed or not borrowed.get("client_id"):
                raise AuthError(
                    f"Provider '{provider.name}' requires OAuth client of "
                    f"'{cfg.borrow_oauth_client_from}' to be configured."
                )

        return AuthSession.external_utility(accounts=accounts, borrowed_oauth_client=borrowed)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_expired(self, token_data: dict) -> bool:
        obtained_at = token_data.get("obtained_at", 0)
        expires_in = token_data.get("expires_in", 3600)
        return time.time() > obtained_at + expires_in - self._token_expiry_buffer

    @staticmethod
    def _oauth2_config(provider: BaseProvider):
        cfg = provider.auth.oauth2
        if not cfg:
            raise AuthError(f"Provider '{provider.name}' is missing OAuth2 configuration")
        return cfg
