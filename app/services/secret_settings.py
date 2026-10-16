"""
The secret settings that providers declare are sealed too (docs/credentials.md), but which of the
settings in the database are secret is known only to the registry: a provider declares it
(``ProviderSetting.secret``), the migration that encrypts the existing credentials cannot ask.
So the settings a provider declared secret and an older version stored in clear are sealed here,
once the providers are known: at the start of the API and each time its registry is rebuilt. A
value written since the upgrade is sealed when it is written (``SettingsService.write_values``),
so this finds nothing to do on a deployment that has gone through it once.

The secret settings of the CATALOG are not touched here: the migration seals them, and one found
in clear later is unreadable (``db/settings_store.py``).
"""
from __future__ import annotations

import logging

from sqlmodel import Session

from core.interfaces import ProviderRegistry
from core.settings_catalog import get_def
from core.settings_resolver import merged_schema
from db.settings_store import SettingsRepository

logger = logging.getLogger(__name__)


def seal_declared_secrets(registry: ProviderRegistry, engine=None) -> int:
    """Seal the stored-in-clear values of the secret settings the providers of ``registry``
    declare; how many were sealed. Never raises: it is a clean-up after an upgrade, and a database
    that does not answer now is asked again at the next reload."""
    keys = [setting.key for setting in merged_schema(registry) if setting.secret and get_def(setting.key) is None]
    if not keys:
        return 0
    try:
        if engine is None:
            from db.engine import engine as shared_engine

            engine = shared_engine
        with Session(engine) as session:
            sealed = SettingsRepository(session).seal_system_in_clear(keys)
    except Exception as exc:  # noqa: BLE001 — see above
        logger.warning("Could not seal the secret settings of the providers: %s", exc)
        return 0
    if sealed:
        logger.info("Sealed %d secret provider setting(s) that were stored in clear.", sealed)
    return sealed
