"""
Signing in through a provider the user has linked (``core/provider_login.py``
has the rules, ``routes/provider_login.py`` the HTTP side).

The two ways in, both generic:

- OAuth2: the browser is sent to the provider (``authorize_url``, a state
  bound to the browser by a nonce), comes back to the callback every OAuth2
  provider already has, and ``finish_oauth2`` turns the authorization into a
  user;
- a login account: the form the provider declares (``credential_fields``) is
  posted and ``finish_credentials`` turns it into a user.

Both end in ``sign_in``: ``BaseProvider.identify_account`` says who the account
is, the user it is linked to is the one that signs in, and the user nobody has
linked is decided by the admin's policy. Nothing here matches by email or name
and nothing here knows a provider.

The browser never receives a session in a URL: the callback hands it a
one-time code (``issue_code``) which ``exchange_code`` turns into the session
(``services.refresh_tokens.start_session``, the same issuing as a password).
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from core import provider_login as rules
from core import user_auth
from core.auth import AccountTarget
from musizeer_sdk.auth import AccountIdentity, AuthFlow, AuthSession
from core.auth_manager import AuthError
from core.provider_login import LoginRefused
from db import user_service
from db.auth_store import CredentialStore
from db.locks import xact_lock
from db.models import User
from services.auth import auth_manager_for
from services.credentials import shared_store, user_store
from services.linked_accounts import credential_fields, unique_providers
from services.provider_calls import provider_call
from services.refresh_tokens import RefreshedSession, start_session

_USERNAME_MAX = 32
_USERNAME_TRIES = 50


@dataclass(frozen=True)
class SignedIn:
    user: User
    created: bool


# ------------------------------------------------------------------
# Which providers
# ------------------------------------------------------------------

def login_providers(registry, session: Session) -> list[dict]:
    """The providers a user can sign in through, from what each declares: the
    way in (``auth_flow``), the form of a login account and, for OAuth2,
    nothing to list until an admin set the app up (there is no client to send
    the browser with)."""
    shared = shared_store(session)
    entries = []
    for provider in unique_providers(registry):
        if not rules.provider_can_login(provider):
            continue
        flow = provider.auth.flow
        if flow not in (AuthFlow.OAUTH2, AuthFlow.CREDENTIALS):
            continue
        if flow == AuthFlow.OAUTH2 and shared.get_oauth2_client_id(provider.name) is None:
            continue
        entries.append({
            "name": provider.name,
            "label": provider.label,
            "auth_flow": flow.value,
            "credential_fields": credential_fields(provider),
        })
    return entries


def login_provider(registry, session: Session, name: str):
    """The provider ``name`` when it is one a user can sign in through now, else None."""
    listed = {entry["name"] for entry in login_providers(registry, session)}
    if name not in listed:
        return None
    return next(p for p in unique_providers(registry) if p.name == name)


# ------------------------------------------------------------------
# Identity
# ------------------------------------------------------------------

def identify(provider, auth_session: AuthSession) -> AccountIdentity:
    """Who the account behind ``auth_session`` is, asked to the provider: a
    failure of the service propagates (``ProviderCallError``), an account it
    does not know is a refusal."""
    with provider_call(provider.name, "provider"):
        identity = provider.identify_account(auth_session)
    if identity is None or not str(identity.external_id).strip():
        raise LoginRefused(rules.ERROR_REFUSED, f"'{provider.name}' did not recognise the account")
    return AccountIdentity(str(identity.external_id), identity.display_name)


def account_id_for_link(provider, store: CredentialStore, user_id: str):
    """What ``AuthManager.complete_oauth2`` asks to store with a personal
    account being linked (``account_id``): the provider's id for the account,
    for a provider that can sign users in (refused, ``AlreadyLinked``, when
    another user linked it); None (nothing asked) otherwise."""
    if not rules.provider_can_login(provider):
        return None

    def account_id(auth_session: AuthSession) -> str:
        external_id = identify(provider, auth_session).external_id
        ensure_not_linked_elsewhere(store, provider.name, external_id, user_id)
        return external_id

    return account_id


def credentials_session(submitted: dict) -> AuthSession:
    """The session of a login account being checked: the submitted fields as the provider reads them."""
    return AuthSession(flow=AuthFlow.CREDENTIALS, extra=dict(submitted))


class AlreadyLinked(Exception):
    """The provider's account is already linked to another user."""


def save_linked_credentials(session: Session, user_id: str, provider, submitted: dict) -> None:
    """Links a login account to the user (the viewer's own, ``PERSONAL``):
    asks the provider who it is when it can sign users in, and refuses an
    account another user already linked (``AlreadyLinked``)."""
    store = user_store(session, user_id)
    external_id = None
    if rules.provider_can_login(provider):
        external_id = identify(provider, credentials_session(submitted)).external_id
        ensure_not_linked_elsewhere(store, provider.name, external_id, user_id)
    try:
        store.save_credentials(provider.name, submitted, AccountTarget.PERSONAL, external_id=external_id)
    except IntegrityError:
        session.rollback()
        raise AlreadyLinked() from None


def ensure_not_linked_elsewhere(store: CredentialStore, provider: str, external_id: str, user_id: str) -> None:
    owner = store.linked_user_id(provider, external_id)
    if owner is not None and owner != user_id:
        raise AlreadyLinked()


# ------------------------------------------------------------------
# OAuth2: out and back
# ------------------------------------------------------------------

def authorize_url(provider, session: Session) -> tuple[str, str]:
    """(the URL to send the browser to, the nonce to put in its cookie). The
    state names the provider and the hash of the nonce."""
    nonce = user_auth.new_login_nonce()
    state = user_auth.create_login_state_token(provider.name, nonce)
    url = auth_manager_for(shared_store(session)).get_authorize_url(provider, state=state)
    return url, nonce


def finish_oauth2(session: Session, provider, code: str, *, new_accounts_policy: str) -> SignedIn:
    """The callback of a sign-in: the code is exchanged, the account identified
    and the user signed in. The state and the nonce are verified by the caller
    (``verify_state``)."""
    auth_manager = auth_manager_for(shared_store(session))
    try:
        token = auth_manager.exchange_oauth2_code(provider, code, AccountTarget.PERSONAL)
        identity = identify(provider, auth_manager.build_oauth2_session(provider, token["access_token"]))
    except AuthError as exc:
        raise LoginRefused(rules.ERROR_REFUSED, str(exc)) from exc
    return sign_in(session, provider, identity, AuthFlow.OAUTH2, token, new_accounts_policy)


def verify_state(state: str, *, provider: str, nonce: str | None) -> None:
    """Raises ``LoginRefused`` unless ``state`` is a sign-in started for
    ``provider`` by the browser that holds ``nonce``."""
    try:
        user_auth.verify_login_state_token(state, provider=provider, nonce=nonce)
    except pyjwt.PyJWTError:
        raise LoginRefused(rules.ERROR_INVALID_STATE, "Invalid or expired sign-in state") from None


# ------------------------------------------------------------------
# A login account
# ------------------------------------------------------------------

def finish_credentials(session: Session, provider, submitted: dict, *, new_accounts_policy: str) -> SignedIn:
    identity = identify(provider, credentials_session(submitted))
    return sign_in(session, provider, identity, AuthFlow.CREDENTIALS, submitted, new_accounts_policy)


def submitted_fields_are_valid(provider, submitted: dict) -> bool:
    """The body is exactly the fields the provider declares, each a non-empty string."""
    wanted = [f.key for f in provider.auth.credential_fields]
    return sorted(submitted) == sorted(wanted) and all(isinstance(submitted[k], str) and submitted[k] for k in wanted)


# ------------------------------------------------------------------
# The user
# ------------------------------------------------------------------

def _save(store: CredentialStore, provider, flow: AuthFlow, data: dict, external_id: str) -> None:
    if flow == AuthFlow.OAUTH2:
        store.save_oauth2_token(provider.name, data, AccountTarget.PERSONAL, external_id=external_id)
    else:
        store.save_credentials(provider.name, data, AccountTarget.PERSONAL, external_id=external_id)


def _usable(user: User) -> User:
    if user.pending_approval:
        raise LoginRefused(rules.ERROR_PENDING, "The account is waiting for an administrator to activate it")
    if not user.is_active:
        raise LoginRefused(rules.ERROR_DEACTIVATED, "Account is deactivated")
    return user


def sign_in(
    session: Session, provider, identity: AccountIdentity, flow: AuthFlow, data: dict, new_accounts_policy: str,
) -> SignedIn:
    """The user the account ``identity`` of ``provider`` is linked to, with the
    account's fresh token or credentials stored back; or, for an id nobody
    linked, the account the policy creates. ``data`` is the OAuth2 token or
    the submitted fields."""
    external_id = identity.external_id
    owner_id = CredentialStore(session).linked_user_id(provider.name, external_id)
    if owner_id is not None:
        return _existing(session, provider, owner_id, flow, data, external_id)

    if not rules.can_create_accounts(new_accounts_policy):
        raise LoginRefused(
            rules.ERROR_DISABLED, "No account is linked to this provider account and creating one is not allowed",
        )

    # Two first sign-ins of the same account at once: one creates, the other finds it.
    xact_lock(session, f"provider-login:{provider.name}:{external_id}")
    owner_id = CredentialStore(session).linked_user_id(provider.name, external_id)
    if owner_id is not None:
        return _existing(session, provider, owner_id, flow, data, external_id)

    user = User(
        username=_free_username(session, identity.display_name or f"{provider.name}-user"),
        # Nobody knows this password: the account is entered through its provider.
        password_hash=user_auth.hash_password(secrets.token_urlsafe(32)),
        role="user",  # the admin role never comes from a provider
        is_active=rules.new_account_is_active(new_accounts_policy),
        pending_approval=new_accounts_policy == rules.POLICY_PENDING,
    )
    session.add(user)
    session.flush()
    try:
        _save(user_store(session, user.id), provider, flow, data, external_id)  # commits the user too
    except IntegrityError:
        session.rollback()
        owner_id = CredentialStore(session).linked_user_id(provider.name, external_id)
        if owner_id is None:
            raise
        return _existing(session, provider, owner_id, flow, data, external_id)
    session.refresh(user)
    return SignedIn(_usable(user), created=True)


def _existing(session: Session, provider, owner_id: str, flow: AuthFlow, data: dict, external_id: str) -> SignedIn:
    user = user_service.get_user_by_id(session, owner_id)
    if user is None:  # a foreign key keeps this from happening; refuse rather than guess
        raise LoginRefused(rules.ERROR_REFUSED, "The linked account has no user")
    _usable(user)
    _save(user_store(session, user.id), provider, flow, data, external_id)
    return SignedIn(user, created=False)


def _free_username(session: Session, wanted: str) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", wanted.lower()).strip("-.") or "user"
    base = base[:_USERNAME_MAX]
    for n in range(1, _USERNAME_TRIES + 1):
        candidate = base if n == 1 else f"{base[:_USERNAME_MAX - len(str(n)) - 1]}-{n}"
        if not user_service.username_taken(session, candidate):
            return candidate
    return f"{base[:_USERNAME_MAX - 9]}-{secrets.token_hex(4)}"


# ------------------------------------------------------------------
# From the browser to a session
# ------------------------------------------------------------------

def issue_code(session: Session, user: User, *, now: datetime | None = None) -> str:
    """The one-time code the callback sends the browser back with."""
    now = now or datetime.now(timezone.utc)
    raw, code_hash = user_auth.new_login_code()
    user_service.store_login_code(
        session, user_id=user.id, code_hash=code_hash,
        expires_at=now + timedelta(seconds=user_auth.LOGIN_CODE_EXPIRE_SECONDS), now=now,
    )
    return raw


def exchange_code(session: Session, code: str, *, now: datetime | None = None) -> RefreshedSession:
    """The session a code is worth, once. A code that is unknown, used or expired
    is a refusal, and so is one of a user deactivated meanwhile."""
    now = now or datetime.now(timezone.utc)
    user_id = user_service.claim_login_code(session, user_auth.hash_refresh_token(code), now=now)
    user = user_service.get_user_by_id(session, user_id) if user_id else None
    if user is None:
        raise LoginRefused(rules.ERROR_INVALID_CODE, "Invalid or expired sign-in code")
    _usable(user)
    return start_session(session, user)
