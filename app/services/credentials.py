"""
The accounts a call of the host uses, and the state of each one.

The routing rule is ONE, declared by each provider (``musizerr_sdk.auth.AccountModel``)
and applied by ``db.auth_store.CredentialStore``: a store is built for a
principal and a call reads the account its provider's model sends it to.
This module only names the principals the host acts as:

- ``user_store``: a signed-in viewer. Their own account when the provider has
  personal accounts and they connected one, else the shared one when the
  provider has it.
- ``shared_store``: the instance itself (host jobs, the routes that manage the
  shared accounts, a viewer who is not signed in): shared accounts only.
- ``own_store``: an action on the user's OWN account (importing their likes,
  taking one back): never a shared account, never an app-only token.

Open mode (authentication off) has ONE owner, the instance's synthetic user
(``SYSTEM_USER_ID``): for it "my own account" and "the instance's shared
account" are the same thing, so its personal account IS the shared one. That is
decided HERE, once (``_principal``), and only while authentication is off: with
it on, the synthetic user is nobody (it cannot sign in) and no user, it
included, reaches the shared row through ``own_store``. Every store of the app
is built by these functions (``tests/test_layering.py``).
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from core.auth import AccountTarget, missing_capabilities
from core.settings_catalog import catalog_defaults
from musizerr_sdk.auth import AccountModel, AuthFlow
from db.auth_store import CredentialStore
from db.models import SYSTEM_USER_ID, User
from db.settings_store import SettingsRepository

logger = logging.getLogger(__name__)


def open_mode(session: Session) -> bool:
    """Whether authentication is off, read from the settings table itself (the
    value the app and every worker share). Anything that keeps it from being
    read is "authentication on": the answer that opens nothing.

    The read runs in a SAVEPOINT: a failure undoes only the read (on
    PostgreSQL it would otherwise abort the caller's whole transaction) and the
    caller's pending work, which this helper does not own, is left as it is."""
    try:
        with session.begin_nested():
            enabled = SettingsRepository(session).get_system("auth_enabled", catalog_defaults()["auth_enabled"])
    except Exception as exc:  # noqa: BLE001 — unreadable settings never open anything
        logger.warning("Could not read auth_enabled, treating authentication as on: %s", exc)
        return False
    return not bool(enabled)


def _principal(session: Session, user_id: str | None, *, own_only: bool) -> CredentialStore:
    """The one place a store is built. The instance's synthetic user is the
    instance itself only while authentication is off."""
    instance_owner = user_id == SYSTEM_USER_ID and open_mode(session)
    return CredentialStore(session, user_id=user_id, own_only=own_only, instance_owner=instance_owner)


def user_store(session: Session, user_id: str | None) -> CredentialStore:
    """The store of the principal ``user_id`` (None: nobody, the shared accounts
    only). With authentication off the principal is the instance's synthetic
    user, whose own account is the shared one."""
    return _principal(session, user_id, own_only=False)


def shared_store(session: Session) -> CredentialStore:
    """The instance acting for itself: only the shared accounts."""
    return CredentialStore(session, user_id=None)


def own_store(session: Session, user_id: str) -> CredentialStore:
    """Only the account ``user_id`` connected: no shared account, no app-only
    token. For an action that must act on the user's own account and never on
    a shared one (taking a like back on the origin provider). In open mode the
    synthetic user's own account is the instance's shared one."""
    return _principal(session, user_id, own_only=True)


def account_holders(session: Session) -> list[User]:
    """The users the host's per-user jobs act for: the active users, or, with
    authentication off, the instance's synthetic user (its row is not active:
    nobody signs in as it, and it stays unlisted and unmanaged)."""
    if open_mode(session):
        system = session.get(User, SYSTEM_USER_ID)
        return [system] if system is not None else []
    return list(session.exec(select(User).where(User.is_active == True, User.id != SYSTEM_USER_ID)).all())  # noqa: E712


def holds_accounts(session: Session, user: User | None) -> bool:
    """Whether the host's per-user jobs act for ``user`` (see ``account_holders``)."""
    if user is None:
        return False
    if user.id == SYSTEM_USER_ID:
        return open_mode(session)
    return bool(user.is_active)


def store_for_target(session: Session, user_id: str, target: AccountTarget) -> CredentialStore:
    """The store a connect/test/disconnect of ``target`` goes through: the
    user's own account, or the instance's shared one."""
    return own_store(session, user_id) if target == AccountTarget.PERSONAL else shared_store(session)


def has_own_account(store: CredentialStore, provider) -> bool:
    """Whether the user of ``store`` connected their own account to
    ``provider``. Only OAuth2 and login accounts can be personal: an API key,
    an external utility account and a provider without login never are."""
    if not provider.auth.account_model.has_personal:
        return False
    if provider.auth.flow == AuthFlow.OAUTH2:
        return store.own_oauth2_token(provider.name) is not None
    if provider.auth.flow == AuthFlow.CREDENTIALS:
        return store.own_credentials(provider.name) is not None
    return False


def has_shared_account(store: CredentialStore, provider) -> bool:
    """Whether the instance has an account of its own on ``provider`` (what the
    host's jobs and the users without their own use)."""
    if not provider.auth.account_model.has_shared:
        return False
    match provider.auth.flow:
        case AuthFlow.OAUTH2:
            return store.shared_oauth2_token(provider.name) is not None
        case AuthFlow.CREDENTIALS:
            return store.shared_credentials(provider.name) is not None
        case AuthFlow.API_KEY:
            return store.get_api_key(provider.name) is not None
        case AuthFlow.EXTERNAL_UTILITY:
            cfg = provider.auth.external_utility
            return bool(cfg and store.get_external_utility_accounts(provider.name, cfg.credential_type))
    return False


def account_username(store: CredentialStore, provider, target: AccountTarget) -> str | None:
    """The username of a login account, to show it back (never a secret)."""
    if provider.auth.flow != AuthFlow.CREDENTIALS:
        return None
    stored = store.own_credentials(provider.name) if target == AccountTarget.PERSONAL else store.shared_credentials(provider.name)
    return stored.data.get("username") if stored else None


def own_missing_capabilities(store: CredentialStore, provider) -> list[str]:
    """The capabilities (``musizerr_sdk.capabilities``) the user's own connection to
    ``provider`` was not granted the permission for: it was made before the
    provider asked for it, and reconnecting grants it. Empty without an own
    connection, or when the token does not list its scopes."""
    if provider.auth.flow != AuthFlow.OAUTH2:
        return []
    stored = store.own_oauth2_token(provider.name)
    return missing_capabilities(provider.auth.oauth2, stored.data) if stored else []


def allowed_targets(model: AccountModel, *, is_admin: bool) -> set[AccountTarget]:
    """The accounts of a provider with ``model`` a principal may connect,
    test and disconnect: their own when the model has personal accounts, the
    shared one when it has one and the principal is an admin."""
    targets: set[AccountTarget] = set()
    if model.has_personal:
        targets.add(AccountTarget.PERSONAL)
    if model.has_shared and is_admin:
        targets.add(AccountTarget.SHARED)
    return targets
