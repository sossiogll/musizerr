"""The effective order of the providers of a category: the ONE place the host reads it from.

Some settings of the catalog ORDER the providers of a category: every ``list`` that declares a
``provider_category`` (``core.settings_catalog.provider_order_settings``: today the order in
which metadata providers are asked, a system default a user may override, and the order in which
download providers are tried). Their stored value is a preference the admin (and a user) may
leave empty, or leave behind while providers come and go. Every reader — the detail routes,
discover, search, the download resolver, ``/providers/public``, the settings the UI shows — asks
THIS module for the order and gets the same answer: the stored list (a user's own where the
setting allows an override and they have one, else the system default) restricted to the
providers of the category the registry runs, then every other one, oldest installation first
(``installed_providers``; ``core/provider_order.py`` holds the rule).

Installing a provider also appends it to the stored system default of every ordering setting whose
category is one of its roles (:func:`sync_default`, called by ``services/sources.py`` in the
install's own transaction), so what is stored matches what everybody sees. Uninstalling rewrites
nothing: the effective order filters the stored one.

The rule is driven by the catalog's declarations (``provider_category``, ``scope``); no provider
and no setting key is named in its logic.
"""
from __future__ import annotations

import json

from sqlmodel import Session, select

from core.interfaces import ProviderRegistry
from core.provider_order import effective_order, in_installation_order
from core.settings_catalog import SettingDef, get_def, provider_order_settings
from db.models import InstalledProvider
from db.settings_store import SettingsRepository
from services.config_manager import ConfigManager

# The settings the readers ask for (``core/settings_catalog.py``).
METADATA_PRIORITY_KEY = "metadata_provider_priority"
DOWNLOAD_ORDER_KEY = "download_provider_order"


def ordering_setting(key: str) -> SettingDef:
    """The catalog's definition of an ordering setting; a key that is not one is a programming error."""
    setting = get_def(key)
    if setting is None or setting.type != "list" or not setting.provider_category:
        raise ValueError(f"'{key}' is not a setting that orders the providers of a category")
    return setting


def installation_sequence(session: Session) -> list[str]:
    """The names of every installed provider, oldest installation first (the id breaks a tie)."""
    rows = session.exec(
        select(InstalledProvider.name).order_by(InstalledProvider.installed_at, InstalledProvider.id)
    ).all()
    return list(rows)


def registered_in_installation_order(registry: ProviderRegistry, session: Session, category: str) -> list[str]:
    """The providers of ``category`` the registry runs, oldest installation first. A provider with no
    installation record (nothing in production: the registry registers what the database installed)
    follows the others, in registry order."""
    return in_installation_order(registry.names_for_category(category), installation_sequence(session))


def effective_provider_order(
    setting: SettingDef | str, registry: ProviderRegistry, config: ConfigManager, session: Session,
    user_id: str | None = None,
) -> list[str]:
    """The order of the providers of ``setting``'s category: every registered one, exactly once, never
    empty while any runs. ``user_id`` (None: the system default, e.g. a guest or a shared cache) only
    counts for a setting that allows a personal override; an empty personal list is no override."""
    if isinstance(setting, str):
        setting = ordering_setting(setting)
    stored = None
    if user_id is not None and setting.scope == "system_with_user_override":
        stored = SettingsRepository(session).get_user(setting.key, user_id) or None
    if stored is None:
        stored = config.get(setting.key)
    return effective_order(stored, registered_in_installation_order(registry, session, setting.provider_category))


def effective_metadata_priority(
    registry: ProviderRegistry, config: ConfigManager, session: Session, user_id: str | None = None,
) -> list[str]:
    """The order in which ``user_id`` (None: the system default) asks the metadata providers."""
    return effective_provider_order(METADATA_PRIORITY_KEY, registry, config, session, user_id)


def effective_download_order(registry: ProviderRegistry, config: ConfigManager, session: Session) -> list[str]:
    """The order in which the download providers are tried (a system setting: nobody overrides it)."""
    return effective_provider_order(DOWNLOAD_ORDER_KEY, registry, config, session)


def installed_in_category(session: Session, category: str) -> list[str]:
    """The installed providers that declare the role ``category``, oldest installation first."""
    rows = session.exec(
        select(InstalledProvider).order_by(InstalledProvider.installed_at, InstalledProvider.id)
    ).all()
    return [row.name for row in rows if category in json.loads(row.roles)]


def sync_default(session: Session, *, reinstalled: str | None = None) -> bool:
    """Bring the STORED system default of every ordering setting in line with the installed providers:
    the stored names that are still installed keep their place, every installed provider of the
    setting's category that it does not name is appended (oldest installation first). A setting is
    written only when that changes its list. Nothing is committed: the caller's transaction (the
    install or update that called it) does.

    ``reinstalled``: a provider that was just installed again after an uninstall — its stale place
    in the lists is dropped so it goes to the end like any new provider.

    The stored lists are read from the database (not ``ConfigManager``) to stay consistent with the
    caller's transaction. Returns whether anything was written."""
    repository = SettingsRepository(session)
    written = False
    for setting in provider_order_settings():
        stored = repository.get_system(setting.key, [])
        stored = [name for name in stored if name != reinstalled] if isinstance(stored, list) else []
        updated = effective_order(stored, installed_in_category(session, setting.provider_category))
        if updated != repository.get_system(setting.key, []):
            repository.set_system(setting.key, updated, commit=False)
            written = True
    return written
