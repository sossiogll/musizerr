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
"""
from __future__ import annotations

from sqlmodel import Session

from core.auth import AccountTarget, missing_capabilities
from musizerr_sdk.auth import AccountModel, AuthFlow
from db.auth_store import CredentialStore


def user_store(session: Session, user_id: str | None) -> CredentialStore:
    """The store of the principal ``user_id`` (None: nobody, the shared accounts
    only). With authentication off the principal is the instance's synthetic
    user, whose own accounts are the ones the single tenant connected."""
    return CredentialStore(session, user_id=user_id)


def shared_store(session: Session) -> CredentialStore:
    """The instance acting for itself: only the shared accounts."""
    return CredentialStore(session, user_id=None)


def own_store(session: Session, user_id: str) -> CredentialStore:
    """Only the account ``user_id`` connected: no shared account, no app-only
    token. For an action that must act on the user's own account and never on
    a shared one (taking a like back on the origin provider)."""
    return CredentialStore(session, user_id=user_id, own_only=True)


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
