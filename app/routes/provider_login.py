"""Sign in through a provider the user has linked (``core/provider_login.py``,
``services/provider_login.py``). Mounted under ``/auth/account``.

The callback of the OAuth2 authorization is the one every OAuth2 provider
already has (``routes/auth.py``): its state says whether it is a linking or a
sign-in, and a sign-in is finished here (``finish_callback``)."""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core import provider_login as rules
from core import user_auth
from musizerr_sdk.auth import AuthFlow
from core.provider_login import LoginRefused
from services import login_rate_limit, provider_login
from services.provider_calls import ProviderCallError

from .deps import ConfigDep, RegistryDep, SessionDep
from .users_auth import TokenResponse, cookie_is_secure

logger = logging.getLogger(__name__)

_FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
# Where the browser lands when the authorization ends: the page that exchanges
# the one-time code for a session, or shows why there is none.
_LANDING = "/login/provider"

router = APIRouter()

_STATUS = {
    rules.ERROR_DISABLED: 403,
    rules.ERROR_PENDING: 403,
    rules.ERROR_DEACTIVATED: 403,
    rules.ERROR_REFUSED: 401,
    rules.ERROR_INVALID_CODE: 401,
    rules.ERROR_INVALID_STATE: 400,
}


def _refusal(refused: LoginRefused) -> HTTPException:
    return HTTPException(
        status_code=_STATUS.get(refused.error_code, 400),
        detail={"error_code": refused.error_code, "error_message": refused.message},
    )


def _require_auth_enabled(config: ConfigDep) -> None:
    if not config.auth_enabled:
        raise HTTPException(status_code=400, detail="Authentication is disabled")


def _provider(name: str, registry, session):
    provider = provider_login.login_provider(registry, session, name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"'{name}' cannot sign users in")
    return provider


class LoginProvider(BaseModel):
    name: str
    label: str
    auth_flow: str
    credential_fields: list[dict]


class LoginProviders(BaseModel):
    providers: list[LoginProvider]


class ExchangeBody(BaseModel):
    code: str


class CredentialsLoginBody(BaseModel):
    """The fields of the provider's login form (``credential_fields``): which ones is the provider's to declare."""

    model_config = {"extra": "allow"}


@router.get("/login-providers", response_model=LoginProviders)
def get_login_providers(registry: RegistryDep, session: SessionDep, config: ConfigDep):
    """The providers a user can sign in through, from what each declares
    (``supports_login``) and the way in (OAuth2, or the form of a login
    account). Public, like the login page it feeds; empty with authentication off."""
    if not config.auth_enabled:
        return LoginProviders(providers=[])
    return LoginProviders(providers=provider_login.login_providers(registry, session))


@router.get("/login/{provider}/start", dependencies=[Depends(login_rate_limit.limit_provider_login)])
def start_login(provider: str, request: Request, registry: RegistryDep, session: SessionDep, config: ConfigDep):
    """Sends the browser to the provider's authorization (OAuth2 providers
    only). The state names the provider and the hash of a nonce the response
    also puts in an HttpOnly cookie: the callback needs both."""
    _require_auth_enabled(config)
    p = _provider(provider, registry, session)
    if p.auth.flow != AuthFlow.OAUTH2:
        raise HTTPException(status_code=400, detail=f"'{provider}' signs in with a form, not an authorization")
    url, nonce = provider_login.authorize_url(p, session)
    response = RedirectResponse(url)
    response.set_cookie(
        key=user_auth.LOGIN_NONCE_COOKIE_NAME,
        value=nonce,
        max_age=user_auth.LOGIN_STATE_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",  # sent on the top-level redirect back from the provider, never on a cross-site subrequest
        secure=cookie_is_secure(request),
        path="/",
    )
    return response


@router.post(
    "/login/{provider}/credentials", response_model=TokenResponse,
    dependencies=[Depends(login_rate_limit.limit_provider_login)],
)
def login_with_credentials(
    provider: str, body: CredentialsLoginBody, registry: RegistryDep, session: SessionDep, config: ConfigDep,
):
    """Signs in with the login account of a provider (the form it declares)."""
    _require_auth_enabled(config)
    p = _provider(provider, registry, session)
    if p.auth.flow != AuthFlow.CREDENTIALS:
        raise HTTPException(status_code=400, detail=f"'{provider}' does not sign in with a login")
    submitted = body.model_dump()
    if not provider_login.submitted_fields_are_valid(p, submitted):
        wanted = [f.key for f in p.auth.credential_fields]
        raise HTTPException(status_code=422, detail=f"Expected the fields: {', '.join(wanted)}")
    try:
        signed = provider_login.finish_credentials(
            session, p, submitted, new_accounts_policy=config.provider_login_new_accounts,
        )
    except LoginRefused as refused:
        raise _refusal(refused) from None
    pair = provider_login.start_session(session, signed.user)
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/login/exchange", response_model=TokenResponse, dependencies=[Depends(login_rate_limit.limit_provider_login)])
def exchange_login_code(body: ExchangeBody, session: SessionDep, config: ConfigDep):
    """The session a one-time code is worth: the code the callback of an
    authorization handed the browser, spent by this call whatever it answers."""
    _require_auth_enabled(config)
    try:
        pair = provider_login.exchange_code(session, body.code)
    except LoginRefused as refused:
        raise _refusal(refused) from None
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


# ------------------------------------------------------------------
# The callback of an authorization that is a sign-in (called by routes/auth.py)
# ------------------------------------------------------------------

def _landing(response_params: dict[str, str]) -> RedirectResponse:
    response = RedirectResponse(f"{_FRONTEND_URL}{_LANDING}?{urlencode(response_params)}")
    response.delete_cookie(user_auth.LOGIN_NONCE_COOKIE_NAME, path="/")
    return response


def finish_callback(
    provider: str, code: Optional[str], state: str, request: Request,
    registry: RegistryDep, session: SessionDep, config: ConfigDep,
) -> RedirectResponse:
    """The browser comes back from the provider: the state and the nonce cookie
    must belong together, then the user signs in and the browser is sent to
    the page that exchanges the one-time code for a session. Every refusal is
    an ``error`` code on that same page (the browser is not an API client)."""
    verdict = login_rate_limit.hit(login_rate_limit.client_address(request))
    if not verdict.allowed:
        return _landing({"error": rules.ERROR_RATE_LIMITED})
    try:
        if not config.auth_enabled:
            raise LoginRefused(rules.ERROR_REFUSED, "Authentication is disabled")
        provider_login.verify_state(state, provider=provider, nonce=request.cookies.get(user_auth.LOGIN_NONCE_COOKIE_NAME))
        p = provider_login.login_provider(registry, session, provider)
        if p is None or p.auth.flow != AuthFlow.OAUTH2:
            raise LoginRefused(rules.ERROR_REFUSED, f"'{provider}' cannot sign users in")
        if not code:  # the user refused at the provider
            raise LoginRefused(rules.ERROR_REFUSED, "The authorization was not granted")
        signed = provider_login.finish_oauth2(
            session, p, code, new_accounts_policy=config.provider_login_new_accounts,
        )
        return _landing({"code": provider_login.issue_code(session, signed.user)})
    except LoginRefused as refused:
        return _landing({"error": refused.error_code})
    except ProviderCallError as exc:
        logger.warning("sign-in through '%s' failed upstream: %s", provider, exc.error)
        return _landing({"error": rules.ERROR_UNAVAILABLE})
    except Exception:  # noqa: BLE001 — the browser must land somewhere readable, never on a 500
        logger.warning("sign-in through '%s' failed", provider, exc_info=True)
        return _landing({"error": rules.ERROR_UNAVAILABLE})
