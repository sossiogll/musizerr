"""
Host-side auth types: which account a write is about, how a credential is
stored, what the token endpoint is sent. What a provider DECLARES about its
authentication (flows, account models, OAuth2 configuration and strategies,
the session it receives) is part of the provider contract: ``musizerr_sdk.auth``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from musizerr_sdk.auth import OAuth2Config


class AccountTarget(str, Enum):
    """Which account a write (connect, test, disconnect) is about: the
    viewer's own, or the instance's shared one (admins only)."""

    PERSONAL = "personal"
    SHARED = "shared"


@dataclass(frozen=True)
class StoredCredential:
    """A credential as it is stored: its data, whose it is (``user_id`` None
    = the instance's shared one) and the exact text it was stored as (what a
    compare-and-swap checks, see ``CredentialStore.replace_oauth2_token``)."""

    id: str
    user_id: str | None
    data: dict
    raw: str

    @property
    def shared(self) -> bool:
        return self.user_id is None


@dataclass(frozen=True)
class OAuth2TokenRequest:
    data: dict[str, Any]
    auth: tuple[str, str] | None = None


def granted_scopes(token_data: dict | None) -> set[str] | None:
    """The scopes a stored OAuth2 token was granted (its ``scope`` field,
    RFC 6749 §5.1), or None when the token does not say."""
    scope = (token_data or {}).get("scope")
    if not isinstance(scope, str):
        return None
    return set(scope.split())


def missing_capabilities(config: OAuth2Config | None, token_data: dict | None) -> list[str]:
    """The capabilities of ``config.capability_scopes`` the token was not
    granted every scope for. A token that does not list its scopes misses
    nothing that can be told: the call itself answers then."""
    granted = granted_scopes(token_data)
    if config is None or granted is None:
        return []
    return [capability for capability, scopes in config.capability_scopes.items() if not set(scopes) <= granted]
