"""
Implementazione host di ``musizeer_sdk.context.ProviderContext``: legge
impostazioni e timeout del provider al momento della chiamata (nessuna
cache), cosi' una modifica dalla UI vale subito senza riavvio.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from musizeer_sdk.context import ContextSnapshot
from services.config_manager import get_config
from core.settings_resolver import provider_config_from_system_settings
from services import public_urls

if TYPE_CHECKING:
    from sqlmodel import Session


def _get_provider_config(provider_name: str, session: "Session | None" = None) -> dict[str, Any]:
    """Config SHORT-key già salvata per ``provider_name`` (sempre a scope di
    sistema: le impostazioni di provider sono deployment-wide, mai per
    utente).

    ``session`` è opzionale: un chiamante che ne ha già una in scope la passa
    per riusarla — nessuna connessione in più. ``HostProviderContext`` non ne
    passa mai una (un provider non ha mai una propria sessione): questa
    funzione ne apre una lei stessa sull'engine condiviso."""
    from db.settings_store import SettingsRepository

    if session is not None:
        return provider_config_from_system_settings(provider_name, SettingsRepository(session).all_system())

    from sqlmodel import Session as _Session

    from db.engine import engine as _prod_engine

    with _Session(_prod_engine) as owned_session:
        return provider_config_from_system_settings(provider_name, SettingsRepository(owned_session).all_system())


class HostProviderContext:
    """The host's side of a provider's ``ProviderContext``. A provider never
    holds it: before each call the host takes a ``snapshot()`` and sends it with
    the call, and the provider's runner builds the provider's own context from it
    (``musizeer_sdk.runner``). The accessors below are for the host (e.g. the
    ``AuthManager`` reads the timeouts and the settings of the provider it
    authenticates)."""

    def __init__(self, provider_name: str) -> None:
        self._provider_name = provider_name

    def snapshot(self) -> ContextSnapshot:
        """The settings and timeouts as they are NOW, and the public URL prefix:
        read at every call, so a change from the UI applies to the next one."""
        config = get_config()
        return ContextSnapshot(
            settings=self.settings(),
            http_timeout=config.http_timeout,
            download_timeout=config.download_timeout,
            connect_timeout=config.connect_timeout,
            stream_start_timeout=config.stream_start_timeout,
            api_prefix=public_urls.api_prefix(),
        )

    def settings(self) -> dict[str, Any]:
        return _get_provider_config(self._provider_name)

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings().get(key, default)

    @property
    def http_timeout(self) -> int:
        return get_config().http_timeout

    @property
    def download_timeout(self) -> int:
        return get_config().download_timeout

    @property
    def connect_timeout(self) -> int:
        return get_config().connect_timeout

    @property
    def stream_start_timeout(self) -> int:
        return get_config().stream_start_timeout

    def cover_url(self, cover_id: str) -> str:
        return public_urls.cover_url(self._provider_name, cover_id)

    def stream_url(self, item_id: str) -> str:
        return public_urls.provider_stream_url(self._provider_name, item_id)

    def preview_url(self, item_id: str) -> str:
        return public_urls.preview_url(self._provider_name, item_id)
