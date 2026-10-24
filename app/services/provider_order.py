"""The effective order of the metadata providers: the ONE place the host reads it from.

``metadata_provider_priority`` is a preference the admin (system default) and each user
(override) may leave empty, or leave behind while providers come and go. Every reader — the
detail routes, discover, search, ``/providers/public``, the settings the UI shows — asks THIS
module for the order, and gets the same answer for the same viewer: the stored list (the user's
own when they have one, else the system default) restricted to the metadata providers the
registry runs, then every other one, oldest installation first (``installed_providers``;
``core/provider_order.py`` holds the rule).

Installing a provider also appends it to the stored system default (:func:`sync_default`,
called by ``services/sources.py`` in the install's own transaction), so what is stored matches
what everybody sees. Uninstalling rewrites nothing: the effective order filters the stored one.
No provider is named here: this works on whatever names the registry and the database hold.
"""
from __future__ import annotations

import json

from sqlmodel import Session, select

from core.provider_order import effective_order, in_installation_order
from core.interfaces import ProviderRegistry
from db.models import InstalledProvider
from db.settings_store import SettingsRepository
from services.config_manager import ConfigManager

# The setting that holds the stored order (``core/settings_catalog.py``) and the role of the
# providers it orders (``musizerr_sdk.manifest.ROLES``).
METADATA_PRIORITY_KEY = "metadata_provider_priority"
METADATA_ROLE = "metadata"


def installation_sequence(session: Session) -> list[str]:
    """The names of every installed provider, oldest installation first (the id breaks a tie)."""
    rows = session.exec(
        select(InstalledProvider.name).order_by(InstalledProvider.installed_at, InstalledProvider.id)
    ).all()
    return list(rows)


def registered_metadata_in_installation_order(registry: ProviderRegistry, session: Session) -> list[str]:
    """The metadata providers the registry runs, oldest installation first. A provider with no
    installation record (nothing in production: the registry registers what the database
    installed) follows the others, in registry order."""
    return in_installation_order((p.name for p in registry.all_metadata()), installation_sequence(session))


def effective_metadata_priority(
    registry: ProviderRegistry, config: ConfigManager, session: Session, user_id: str | None = None,
) -> list[str]:
    """The order in which ``user_id`` (None: the system default, e.g. a guest or a shared cache)
    asks the metadata providers: every registered one, exactly once, never empty while any runs.
    An empty personal list is no override."""
    stored = None
    if user_id is not None:
        stored = SettingsRepository(session).get_user(METADATA_PRIORITY_KEY, user_id) or None
    if stored is None:
        stored = config.get(METADATA_PRIORITY_KEY)
    return effective_order(stored, registered_metadata_in_installation_order(registry, session))


def installed_metadata_names(session: Session) -> list[str]:
    """The installed providers that declare the metadata role, oldest installation first."""
    rows = session.exec(
        select(InstalledProvider).order_by(InstalledProvider.installed_at, InstalledProvider.id)
    ).all()
    return [row.name for row in rows if METADATA_ROLE in json.loads(row.roles)]


def sync_default(session: Session, *, reinstalled: str | None = None) -> bool:
    """Bring the STORED system default in line with the installed metadata providers: the stored
    names that are still installed keep their place, every installed one it does not name is
    appended (oldest installation first). Writes only when that changes the stored list, and does
    not commit: the caller's transaction (the install or update that called it) does.

    ``reinstalled``: a provider that was just installed again after an uninstall — its stale place
    in the list is dropped so it goes to the end like any new provider.

    Reading the stored list from the database (not ``ConfigManager``) keeps it consistent with the
    caller's transaction. Returns whether a write was made."""
    repository = SettingsRepository(session)
    stored = repository.get_system(METADATA_PRIORITY_KEY, [])
    stored = [name for name in stored if name != reinstalled] if isinstance(stored, list) else []
    updated = effective_order(stored, installed_metadata_names(session))
    if updated == repository.get_system(METADATA_PRIORITY_KEY, []):
        return False
    repository.set_system(METADATA_PRIORITY_KEY, updated, commit=False)
    return True
