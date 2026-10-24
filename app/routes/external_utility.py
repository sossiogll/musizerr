"""
The intake of the credentials of an external utility.

A provider whose account is captured by a program of its own
(``AuthFlow.EXTERNAL_UTILITY``) declares in its ``ExternalUtilityConfig`` the
name that program uses in the paths of its calls (``intake_name``). This module
serves those paths for whichever provider declares the name; it knows no
provider and no utility. The utility sends the account of a session it captured
and the host keeps it for the provider (``AuthSession.extra["accounts"]``):

- ``GET /credentials/{name}``: the accounts held, shown with the provider's
  ``account_display_fields``;
- ``POST|DELETE /credentials/{name}/{account}``: register (any JSON object, kept
  as received) or remove one;
- ``GET|PUT /credentials/{name}_api_config``: when the provider borrows another
  provider's OAuth client, whether it is set up, and set it up.

Every route is admin-only (``AdminUserDep``): the utility signs in through the
host's own login and sends the access token as a Bearer header on every call,
so an admin account works unchanged. With auth disabled the built-in admin
applies, as for every admin route. The OAuth client secret is never returned:
the utility only checks that it is set. Mounted under ``/api`` too
(``main.py``): the utility hardcodes that prefix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.settings_resolver import PROVIDER_CATEGORY_GETTERS
from musizeer_sdk.auth import AuthFlow, ExternalUtilityConfig

from .deps import AdminUserDep, RegistryDep, SharedStoreDep

router = APIRouter()

_SECRET_MASK = "********"


class OAuthClientBody(BaseModel):
    client_id: str
    client_secret: str


def _utility(registry, intake_name: str) -> tuple[Any, ExternalUtilityConfig]:
    """The provider that declares ``intake_name`` and its utility's configuration
    (404 when nobody does: the path then does not exist)."""
    for getter in PROVIDER_CATEGORY_GETTERS:
        for provider in getattr(registry, getter)():
            config = provider.auth.external_utility
            if provider.auth.flow == AuthFlow.EXTERNAL_UTILITY and config and config.intake_name == intake_name:
                return provider, config
    raise HTTPException(status_code=404, detail=f"No provider takes credentials as '{intake_name}'")


def _oauth_client_provider(registry, intake_name: str) -> str:
    _provider, config = _utility(registry, intake_name)
    if not config.borrow_oauth_client_from:
        raise HTTPException(status_code=404, detail=f"'{intake_name}' does not borrow an OAuth client")
    return config.borrow_oauth_client_from


def _shown(config: ExternalUtilityConfig, payload: dict) -> dict:
    return {field.key: payload.get(field.key, "") for field in config.account_display_fields}


# Registered before "/credentials/{intake_name}", which would otherwise take the
# "<name>_api_config" path for a name of its own.

@router.get("/credentials/{intake_name}_api_config")
def get_oauth_client(intake_name: str, registry: RegistryDep, store: SharedStoreDep, _admin: AdminUserDep):
    """
    Tells the utility whether the host has the borrowed OAuth client configured
    before it walks the user through the capture. The secret itself is masked:
    the utility only checks that both fields are set.
    """
    client = store.get_oauth2_client(_oauth_client_provider(registry, intake_name)) or {}
    has_secret = bool(client.get("client_secret"))
    return {
        "client_id": client.get("client_id", ""),
        "client_secret": _SECRET_MASK if has_secret else "",
        "configured": bool(client.get("client_id")) and has_secret,
    }


@router.put("/credentials/{intake_name}_api_config")
def put_oauth_client(
    intake_name: str, body: OAuthClientBody, registry: RegistryDep, store: SharedStoreDep, _admin: AdminUserDep,
):
    """
    Lets the utility populate the borrowed OAuth client when missing. Writes to
    the same store the provider the client belongs to reads from.
    """
    store.save_oauth2_client(_oauth_client_provider(registry, intake_name), body.client_id, body.client_secret)
    return {"ok": True}


@router.get("/credentials/{intake_name}")
def list_accounts(intake_name: str, registry: RegistryDep, store: SharedStoreDep, _admin: AdminUserDep):
    provider, config = _utility(registry, intake_name)
    accounts = store.get_external_utility_accounts(provider.name, config.credential_type)
    return [{"name": name, **_shown(config, data)} for name, data in accounts.items()]


@router.post("/credentials/{intake_name}/{account_name}")
def register_account(
    intake_name: str,
    account_name: str,
    body: dict[str, Any],
    registry: RegistryDep,
    store: SharedStoreDep,
    _admin: AdminUserDep,
):
    """
    Called by the utility at the end of its capture flow. Keeps the object it
    sends, as received, with the time of registration, for the provider.
    """
    provider, config = _utility(registry, intake_name)
    if not account_name.strip():
        raise HTTPException(status_code=400, detail="account_name cannot be empty")

    payload = {**body, "registered_at": datetime.now(timezone.utc).isoformat()}
    store.save_external_utility_account(provider.name, config.credential_type, account_name, payload)
    return {"ok": True, "account": account_name, **_shown(config, payload)}


@router.delete("/credentials/{intake_name}/{account_name}")
def delete_account(
    intake_name: str, account_name: str, registry: RegistryDep, store: SharedStoreDep, _admin: AdminUserDep,
):
    provider, config = _utility(registry, intake_name)
    removed = store.delete_external_utility_account(provider.name, config.credential_type, account_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
    return {"ok": True}
