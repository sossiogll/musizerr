"""
Signing in through a provider the user has linked: the pure rules, no I/O.

A user links an account of a provider from an open session (Settings > Linked
accounts) and the host stores the provider's stable id for it. Signing in
through the provider finds the user by that id, and only by it: an email, a
name or a username never links two accounts.

What happens the FIRST time an id that no user has linked comes to sign in is
the admin's policy (``POLICY_SETTING_KEY``, the ``provider_login_new_accounts``
setting):

- ``disabled`` (the default): nothing is created, the sign-in is refused;
- ``pending``: an account is created and waits for an admin to activate it;
- ``active``: an account is created and the user is signed in at once.

The account created is always a plain ``user``: the admin role never comes from
a provider. Provider-agnostic: nothing here knows a provider.
"""
from __future__ import annotations

POLICY_DISABLED = "disabled"
POLICY_PENDING = "pending"
POLICY_ACTIVE = "active"
POLICIES = (POLICY_DISABLED, POLICY_PENDING, POLICY_ACTIVE)
# Closed unless an admin opens it.
DEFAULT_POLICY = POLICY_DISABLED
POLICY_SETTING_KEY = "provider_login_new_accounts"

# Why a sign-in through a provider was refused: the stable ``error_code`` of the
# API answers and of the ``?provider_login_error=`` the browser lands on.
ERROR_DISABLED = "provider_login_disabled"
ERROR_PENDING = "account_pending"
ERROR_DEACTIVATED = "account_deactivated"
ERROR_REFUSED = "provider_login_refused"
ERROR_INVALID_STATE = "provider_login_invalid_state"
ERROR_INVALID_CODE = "provider_login_invalid_code"
ERROR_UNAVAILABLE = "provider_login_unavailable"
ERROR_RATE_LIMITED = "provider_login_rate_limited"


class LoginRefused(Exception):
    """A sign-in through a provider that cannot go on: ``error_code`` is the stable code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def can_create_accounts(policy: str) -> bool:
    return policy in (POLICY_PENDING, POLICY_ACTIVE)


def new_account_is_active(policy: str) -> bool:
    """Whether the account a first sign-in creates can be used at once."""
    return policy == POLICY_ACTIVE


def provider_can_login(provider) -> bool:
    """A provider can sign users in when it declares it (``supports_login``)
    AND has accounts of a user's own: a shared account belongs to nobody."""
    return provider.auth.account_model.has_personal and provider.supports_login()
