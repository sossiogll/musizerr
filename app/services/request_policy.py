"""
Where the approval policy of download requests (``core/download_policy.py``)
comes from, at both levels:

- the GENERAL policy is a group of settings of the catalogue
  (``core/settings_catalog.py``, ``download_request_*``): admin-only, written
  through ``SettingsService`` like every other setting;
- a user's OVERRIDE (a mode or threshold per kind, the quota) is a row of
  ``user_request_policies``, written only by an admin, for another user.

Why not settings rows of scope ``user`` for the override: the settings
pipeline attributes a ``user`` row to whoever writes it (the user themselves,
the whole point of that scope) and ``system_with_user_override`` lets every
user set their own, which is exactly what a user must NOT do with their own
approval policy. Letting an admin write someone else's row, and nobody write
their own, would need a new scope and a target-user parameter through the
whole write pipeline, the schema endpoint and the UI; and "inherit" is a
per-field state (a mode without a threshold) that a typed row expresses
directly. So the override is a small table of its own, and the admin routes
that write it check the role.

Who is subject to the policy is decided here: a user with authentication on.
Admins never are, and with authentication off (open mode) nobody is: there are
no individual users to govern, only the built-in administrator.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session

from core import download_policy as policy
from db import download_request_store as store
from db.models import SYSTEM_USER_ID, User
from services.config_manager import ConfigManager
from services.settings import SettingsService


def governed(user: User, config: ConfigManager) -> bool:
    """Whether ``user``'s download requests go through the policy: only a
    signed-in non-admin. Admins and open mode (authentication off: everyone
    is the built-in administrator) are never subject to it."""
    return bool(config.auth_enabled) and user.role != "admin" and user.id != SYSTEM_USER_ID


def general_policy(config: ConfigManager) -> policy.Policy:
    """The policy the admin set in the settings."""
    return policy.Policy.from_settings(config.get)


def policy_for(session: Session, config: ConfigManager, user_id: str) -> policy.Policy:
    """The policy in force for ``user_id``: the general one with the user's
    override on top."""
    return policy.effective_policy(general_policy(config), store.get_override(session, user_id))


def public_policy(value: policy.Policy) -> dict[str, Any]:
    """A policy as the API shows it."""
    return {
        "rules": {kind: {"mode": rule.mode, "threshold": rule.threshold} for kind, rule in value.rules.items()},
        "quota": {"tracks": value.quota_tracks, "window_days": value.quota_days},
    }


def read_general(config: ConfigManager) -> dict[str, Any]:
    return public_policy(general_policy(config))


def write_general(session: Session, config: ConfigManager, registry, admin: User, patch: dict[str, Any]) -> dict[str, Any]:
    """Applies a partial general policy (``{"rules": {kind: {"mode",
    "threshold"}}, "quota": {"tracks", "window_days"}}``) through the
    settings catalogue, as ONE atomic write: it is validated like any setting
    (``SettingsService.write_values``), a ``SettingsWriteError`` refuses the
    whole patch. Returns the policy now in force."""
    updates: dict[str, Any] = {}
    for kind, rule in (patch.get("rules") or {}).items():
        if rule.get("mode") is not None:
            updates[policy.mode_key(kind)] = rule["mode"]
        if rule.get("threshold") is not None:
            updates[policy.threshold_key(kind)] = rule["threshold"]
    quota = patch.get("quota") or {}
    if quota.get("tracks") is not None:
        updates[policy.QUOTA_TRACKS_KEY] = quota["tracks"]
    if quota.get("window_days") is not None:
        updates[policy.QUOTA_DAYS_KEY] = quota["window_days"]
    if updates:
        SettingsService(session, config, registry).write_values(updates, admin)
    return read_general(config)


def read_user(session: Session, config: ConfigManager, user_id: str) -> dict[str, Any]:
    """What the admin changed for ``user_id`` and the policy that results."""
    override = store.get_override(session, user_id)
    return {
        "override": override.as_dict(),
        "effective": public_policy(policy.effective_policy(general_policy(config), override)),
    }


def write_user(session: Session, config: ConfigManager, user_id: str, data: dict[str, Any], at) -> dict[str, Any]:
    """Replaces the user's override with ``data`` (``PolicyOverride.from_dict``:
    ``ValueError`` when it is not valid; an empty one clears it)."""
    override = policy.PolicyOverride.from_dict(data)
    if override.is_empty:
        store.clear_override(session, user_id)
    else:
        store.set_override(session, user_id, override, at)
    return read_user(session, config, user_id)


def clear_user(session: Session, config: ConfigManager, user_id: str) -> dict[str, Any]:
    store.clear_override(session, user_id)
    return read_user(session, config, user_id)
