from __future__ import annotations
import os
from typing import Annotated, Literal, Optional

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from core.auth import AccountTarget
from musizerr_sdk.auth import AuthFlow
from core.auth_manager import AuthError
from core.auth_strategies import build_fallback_session
from core.settings_resolver import find_provider_by_name
from core.provider_login import LoginRefused
from core.user_auth import create_oauth_state_token, decode_oauth_state_token, state_token_type
from db.models import User
from db import user_service
from services.auth import auth_manager_for
from services.credentials import allowed_targets, own_store, shared_store, store_for_target, user_store
from services import provider_login
from services.errors import public_error
from services.linked_accounts import linked_accounts
from .deps import AdminUserDep, ConfigDep, CurrentUserDep, RegistryDep, SessionDep, SharedStoreDep
from .provider_login import finish_callback as finish_login_callback
from .users_auth import LoginBody, login as account_login

_FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")

router = APIRouter()


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------

class ApiKeyBody(BaseModel):
    api_key: str


class OAuthClientBody(BaseModel):
    client_id: str
    client_secret: str


class CredentialsBody(BaseModel):
    """The fields of the provider's login form (``credential_fields`` of its
    linked account): which ones is the provider's to declare."""

    model_config = {"extra": "allow"}


# ------------------------------------------------------------------
# Global auth status: whether this instance requires a login, for external
# tools that probe it before logging in through /auth/login.
# ------------------------------------------------------------------

@router.get("/status")
def auth_status_global(config: ConfigDep):
    return {
        "auth_enabled": config.auth_enabled,
        "authenticated": not config.auth_enabled,
        "registration_open": config.registration_open,
    }


@router.post("/login")
def login_for_external_tools(body: LoginBody, session: SessionDep, config: ConfigDep):
    """Login per i tool esterni che chiamano ``/auth/login``: stesse regole
    di ``/auth/account/login``, più nome e ruolo dell'utente."""
    tokens = account_login(body, session, config)
    user = user_service.get_user_by_username(session, body.username)
    return {**tokens.model_dump(), "user": {"username": user.username, "role": user.role}}


# ------------------------------------------------------------------
# Which account a write is about
# ------------------------------------------------------------------

TargetParam = Annotated[
    Optional[AccountTarget],
    Query(
        description="The account the call is about: your own (`personal`) or the instance's shared one "
        "(`shared`, admins only). Omitted: your own when the provider has personal accounts, else the shared one.",
    ),
]


def _resolve_target(provider, user: User, target: Optional[AccountTarget]) -> AccountTarget:
    """The account ``user`` acts on, or the refusal: 400 when the provider's
    declared model has no such account, 403 when it has one the user may not manage."""
    model = provider.auth.account_model
    if target is None:
        target = AccountTarget.PERSONAL if model.has_personal else AccountTarget.SHARED
    has_account = model.has_personal if target == AccountTarget.PERSONAL else model.has_shared
    if not has_account:
        raise HTTPException(status_code=400, detail=f"'{provider.name}' has no {target.value} account")
    if target not in allowed_targets(model, is_admin=user.role == "admin"):
        raise HTTPException(status_code=403, detail="Admin role required for the shared account")
    return target


# ------------------------------------------------------------------
# Linked accounts of the viewer
# ------------------------------------------------------------------

@router.get("/linked-accounts")
def get_linked_accounts(registry: RegistryDep, session: SessionDep, user: CurrentUserDep):
    """The providers the viewer may connect their own account to, with what is
    connected: from each provider's declared account model, never a list of names."""
    return {"accounts": linked_accounts(registry, session, user.id)}


# ------------------------------------------------------------------
# OAuth2 flow
# ------------------------------------------------------------------

def _authorize_url(provider: str, target: Optional[AccountTarget], registry, session, user) -> str:
    p = _get_provider(provider, registry)
    resolved = _resolve_target(p, user, target)
    auth_manager = auth_manager_for(shared_store(session))
    try:
        return auth_manager.get_authorize_url(p, state=create_oauth_state_token(user.id, resolved.value))
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{provider}/authorize-url")
def get_authorize_url(
    provider: str, registry: RegistryDep, session: SessionDep, user: CurrentUserDep,
    target: TargetParam = None,
):
    """Returns the OAuth2 authorization URL as JSON. The frontend redirects the browser there."""
    return {"url": _authorize_url(provider, target, registry, session, user)}


@router.get("/{provider}/start")
def auth_start(
    provider: str, registry: RegistryDep, session: SessionDep, user: CurrentUserDep,
    target: TargetParam = None,
):
    return RedirectResponse(_authorize_url(provider, target, registry, session, user))


@router.get("/{provider}/callback")
def auth_callback(
    provider: str,
    state: str,
    request: Request,
    registry: RegistryDep,
    session: SessionDep,
    config: ConfigDep,
    code: Optional[str] = None,
):
    """OAuth2 callback — browser redirect from the provider, no JWT available.
    The state says which flow this is. A linking carries the user and the
    account (own or shared) in its signed state, and the rule that let them
    start the flow is checked again here; a sign-in carries the provider and
    the hash of the nonce of the browser that started it, and ends on the page
    that exchanges a one-time code for a session (``routes/provider_login.py``)."""
    try:
        kind = state_token_type(state)
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    if kind == "oauth_login_state":
        return finish_login_callback(provider, code, state, request, registry, session, config)
    if not code:
        raise HTTPException(status_code=400, detail="The authorization was not granted")

    try:
        user_id, raw_target = decode_oauth_state_token(state)
        target = AccountTarget(raw_target)
    except (pyjwt.PyJWTError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    user = session.exec(select(User).where(User.id == user_id)).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (user not found)")

    p = _get_provider(provider, registry)
    _resolve_target(p, user, target)
    store = store_for_target(session, user.id, target)
    auth_manager = auth_manager_for(store)
    try:
        auth_manager.complete_oauth2(p, code, target, account_id=provider_login.account_id_for_link(p, store, user.id))
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LoginRefused as refused:
        raise HTTPException(status_code=400, detail=refused.message)
    except (provider_login.AlreadyLinked, IntegrityError):
        session.rollback()
        raise HTTPException(status_code=409, detail="This account is already linked to another user")
    landing = "/admin/providers" if target == AccountTarget.SHARED else "/config/linked-accounts"
    return RedirectResponse(f"{_FRONTEND_URL}{landing}?auth=success")


@router.get("/{provider}/status")
def auth_status(provider: str, registry: RegistryDep, session: SessionDep, user: CurrentUserDep):
    """Whether the viewer's calls to the provider authenticate (the account the routing picks for them)."""
    p = _get_provider(provider, registry)
    auth_manager = auth_manager_for(user_store(session, user.id))
    try:
        auth_manager.authenticate(p)
        authenticated = True
    except AuthError:
        authenticated = False
    return {"provider": provider, "authenticated": authenticated}


# ------------------------------------------------------------------
# API key — the instance's, admin-only
# ------------------------------------------------------------------

@router.post("/{provider}/apikey")
def save_api_key(provider: str, body: ApiKeyBody, registry: RegistryDep, store: SharedStoreDep, _admin: AdminUserDep):  # noqa: ARG001
    p = _get_provider(provider, registry)
    if p.auth.flow != AuthFlow.API_KEY:
        raise HTTPException(status_code=400, detail=f"'{provider}' does not use an API key")
    store.save_api_key(provider, body.api_key)
    return {"ok": True}


# ------------------------------------------------------------------
# OAuth2 client credentials — the instance's, admin-only
# ------------------------------------------------------------------

@router.post("/{provider}/client")
def save_oauth_client(
    provider: str,
    body: OAuthClientBody,
    registry: RegistryDep,
    store: SharedStoreDep,
    _admin: AdminUserDep,  # noqa: ARG001
):
    p = _get_provider(provider, registry)
    if p.auth.flow != AuthFlow.OAUTH2:
        raise HTTPException(status_code=400, detail=f"'{provider}' does not use OAuth2")
    store.save_oauth2_client(provider, body.client_id, body.client_secret)
    return {"ok": True}


@router.delete("/{provider}/client")
def delete_oauth_client(
    provider: str,
    registry: RegistryDep,
    store: SharedStoreDep,
    _admin: AdminUserDep,  # noqa: ARG001
):
    p = _get_provider(provider, registry)
    if p.auth.flow != AuthFlow.OAUTH2:
        raise HTTPException(status_code=400, detail=f"'{provider}' does not use OAuth2")
    store.delete_oauth2_client(provider)
    return {"ok": True}


# ------------------------------------------------------------------
# Login accounts — the form is the provider's (``credential_fields``)
# ------------------------------------------------------------------

@router.post("/{provider}/credentials")
def save_credentials(
    provider: str,
    body: CredentialsBody,
    registry: RegistryDep,
    session: SessionDep,
    user: CurrentUserDep,
    target: TargetParam = None,
):
    p = _get_provider(provider, registry)
    if p.auth.flow != AuthFlow.CREDENTIALS:
        raise HTTPException(status_code=400, detail=f"'{provider}' does not use a login")
    resolved = _resolve_target(p, user, target)
    submitted = body.model_dump()
    wanted = [f.key for f in p.auth.credential_fields]
    if sorted(submitted) != sorted(wanted) or not all(isinstance(submitted[k], str) and submitted[k] for k in wanted):
        raise HTTPException(status_code=422, detail=f"Expected the fields: {', '.join(wanted)}")
    if resolved == AccountTarget.PERSONAL:
        try:
            provider_login.save_linked_credentials(session, user.id, p, submitted)
        except LoginRefused as refused:
            raise HTTPException(status_code=422, detail=refused.message)
        except provider_login.AlreadyLinked:
            raise HTTPException(status_code=409, detail="This account is already linked to another user")
    else:
        store_for_target(session, user.id, resolved).save_credentials(provider, submitted, resolved)
    return {"success": True}


# ------------------------------------------------------------------
# Test connection
# ------------------------------------------------------------------

@router.post("/{provider}/test")
def test_connection(
    provider: str,
    registry: RegistryDep,
    session: SessionDep,
    user: CurrentUserDep,
    scope: Annotated[Literal["app", "account"], Query()] = "account",
    target: Annotated[
        Optional[AccountTarget],
        Query(
            description="With `scope=account`: test your own account (`personal`) or the shared one (`shared`, "
            "admins). Omitted: the account your own calls would use.",
        ),
    ] = None,
):
    p = _get_provider(provider, registry)
    is_admin = user.role == "admin"
    show_detail = is_admin

    def failure(raw: str) -> dict:
        body = public_error(raw, "provider", show_detail=show_detail)
        return {"ok": False, "detail": raw if show_detail else body["error_message"], **body}

    result: dict = {"app": None, "account": None}

    # The app client (OAuth2 only) is the instance's: admin-only, it exposes whether it is configured.
    if scope == "app":
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin role required")
        store = shared_store(session)
        auth_manager = auth_manager_for(store)
        try:
            cfg = p.auth.oauth2 if p.auth.flow == AuthFlow.OAUTH2 else None
            if cfg and cfg.fallback_auth:
                session_obj = build_fallback_session(cfg.fallback_auth, auth_manager, p)
                if session_obj:
                    err = p.test_connection(session_obj)
                    result["app"] = failure(err) if err else {"ok": True, "detail": "Connected"}
                else:
                    result["app"] = {"ok": False, "detail": "No app credentials configured"}
            else:
                result["app"] = {"ok": False, "detail": "Provider has no app-level auth"}
        except Exception as e:
            result["app"] = failure(str(e))
        return result

    if target is not None:
        resolved = _resolve_target(p, user, target)
        store = store_for_target(session, user.id, resolved)
    else:
        # What the viewer's own calls use (the same answer GET /providers gives as `authenticated`).
        store = user_store(session, user.id)
    try:
        session_obj = auth_manager_for(store).authenticate(p)
        err = p.test_connection(session_obj)
        result["account"] = failure(err) if err else {"ok": True, "detail": "Connected"}
    except Exception as e:
        result["account"] = failure(str(e))
    return result


# ------------------------------------------------------------------
# Disconnect
# ------------------------------------------------------------------

@router.delete("/{provider}")
def revoke_auth(
    provider: str,
    registry: RegistryDep,
    session: SessionDep,
    user: CurrentUserDep,
    target: TargetParam = None,
):
    """Disconnects an account: the viewer's own, or (admin) the instance's shared one."""
    p = _get_provider(provider, registry)
    resolved = _resolve_target(p, user, target)
    store = store_for_target(session, user.id, resolved)
    match p.auth.flow:
        case AuthFlow.OAUTH2:
            store.delete_oauth2_token(provider, resolved)
        case AuthFlow.CREDENTIALS:
            store.delete_credentials(provider, resolved)
        case AuthFlow.API_KEY:
            store.delete_api_key(provider)
        case _:
            raise HTTPException(status_code=400, detail=f"'{provider}' has no account to disconnect here")
    return {"ok": True}


@router.delete("/me/accounts")
def revoke_all_auth(session: SessionDep, user: CurrentUserDep):
    """Every account the viewer connected, in one action. Only the viewer's
    own: the shared accounts of the instance are never touched."""
    return {"ok": True, "removed": own_store(session, user.id).delete_all_personal()}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_provider(name: str, registry):
    # Generic lookup over EVERY category the registry declares
    # (core.settings_resolver.find_provider_by_name), shared with routes/config.py.
    p = find_provider_by_name(registry, name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return p
