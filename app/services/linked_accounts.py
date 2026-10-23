"""
The accounts of each provider as a person sees them: the viewer's own (what
they connected) and the instance's shared one (what an admin manages), both
read through the one routing rule (``db.auth_store.CredentialStore``) and
worded by the provider's declared model (``musizerr_sdk.auth.AccountModel``).

Used by GET /auth/linked-accounts (what a user can connect) and by GET
/providers (what an admin manages): the same state, never two computations.
"""
from __future__ import annotations

from sqlmodel import Session

from core.auth import AccountTarget
from musizerr_sdk.auth import AccountModel, AuthFlow
from core.provider_login import provider_can_login
from core.interfaces import personal_capabilities
from core.settings_resolver import PROVIDER_CATEGORY_GETTERS
from services.credentials import (
    account_username,
    has_own_account,
    has_shared_account,
    own_missing_capabilities,
    own_store,
    shared_store,
)


def unique_providers(registry) -> list:
    """Every registered provider once, whatever categories it is in (a dual
    provider is one object): by identity, never by name, which two distinct
    providers may share."""
    seen: set[int] = set()
    providers = []
    for getter_name in PROVIDER_CATEGORY_GETTERS:
        for provider in getattr(registry, getter_name)():
            if id(provider) not in seen:
                seen.add(id(provider))
                providers.append(provider)
    return providers


def credential_fields(provider) -> list[dict]:
    """The form of a login account (empty for any other flow)."""
    if provider.auth.flow != AuthFlow.CREDENTIALS:
        return []
    return [{"key": f.key, "label": f.label, "secret": f.secret} for f in provider.auth.credential_fields]


def personal_state(provider, own) -> dict | None:
    """The viewer's own account of ``provider`` (``own``: ``own_store``), or
    None when the provider has no personal accounts."""
    if not provider.auth.account_model.has_personal:
        return None
    connected = has_own_account(own, provider)
    return {
        "connected": connected,
        "username": account_username(own, provider, AccountTarget.PERSONAL) if connected else None,
        # What reconnecting would grant: the permissions the provider asks for now and this account lacks.
        "missing_permissions": own_missing_capabilities(own, provider) if connected else [],
        # What this account enables, declared by the provider (``musizerr_sdk.capabilities``).
        "capabilities": personal_capabilities(provider),
        # Whether the user can sign in through this account (the provider says who it is
        # and it was linked since): null when the provider cannot sign anybody in, false
        # when it can and this account must be reconnected (linked before, or never identified).
        # The instance itself (authentication off) signs nobody in: null.
        "login": (
            (connected and own.own_external_id(provider.name) is not None)
            if provider_can_login(provider) and not own.instance_owner else None
        ),
    }


def shared_state(provider, shared, *, is_admin: bool) -> dict | None:
    """The instance's account of ``provider`` (``shared_store``), or None when
    the provider has none. Who it is stays with the admins."""
    if not provider.auth.account_model.has_shared:
        return None
    connected = has_shared_account(shared, provider)
    return {
        "connected": connected,
        "username": account_username(shared, provider, AccountTarget.SHARED) if connected and is_admin else None,
    }


def linked_accounts(registry, session: Session, user_id: str) -> list[dict]:
    """The providers ``user_id`` may connect their own account to, with the
    state of each: generic, built only from what each provider declares."""
    own = own_store(session, user_id)
    shared = shared_store(session)
    accounts = []
    for provider in unique_providers(registry):
        model = provider.auth.account_model
        if not model.has_personal:
            continue
        accounts.append({
            "name": provider.name,
            "label": provider.label,
            "auth_flow": provider.auth.flow.value,
            "account_model": model.value,
            # A login on an OAuth2 service needs the instance's app client: without it there is nothing to connect.
            "connectable": provider.auth.flow != AuthFlow.OAUTH2 or shared.get_oauth2_client_id(provider.name) is not None,
            "credential_fields": credential_fields(provider),
            "personal": personal_state(provider, own),
            "shared": shared_state(provider, shared, is_admin=False) if model == AccountModel.PERSONAL_WITH_SHARED else None,
        })
    return accounts
