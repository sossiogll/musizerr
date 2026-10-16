"""
What the host knows about providers beyond the contract itself. The contract
(the abstract base classes and the data types that cross the boundary with a
provider) is ``musizerr_sdk.interfaces``; this module holds the registry the
core asks for providers through and the host's reading of the capabilities
they declare.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from musizerr_sdk.capabilities import (
    CAPABILITY_DISCOGRAPHY,
    CAPABILITY_LOGIN,
    CAPABILITY_PLAYLIST_TRACKS_RESTRICTED,
    CAPABILITY_PROPAGATE_FAVORITES,
    CAPABILITY_PROPAGATE_PLAYLISTS,
    CAPABILITY_UNLIKE_SAVED_TRACK,
    CAPABILITY_USER_LIBRARY,
)
from musizerr_sdk.interfaces import (
    BaseProvider,
    DownloadProvider,
    IdentificationProvider,
    LyricsProvider,
    MetadataProvider,
    StorageProvider,
    SyncProvider,
)


@dataclass
class SyncAvailability:
    """Where a track is in a sync provider's library, as the HOST decided it
    (``services.enrichment.check_sync_availability`` judges the candidates the
    provider found, ``SyncProvider.find_candidates``)."""

    sync_id: str
    stream_url: str | None = None


def metadata_capabilities(provider: MetadataProvider) -> list[str]:
    """The capability keys ``provider`` declares, in a stable order."""
    declared = (
        (CAPABILITY_USER_LIBRARY, provider.supports_user_library),
        (CAPABILITY_UNLIKE_SAVED_TRACK, provider.supports_unlike_saved_track),
        (CAPABILITY_DISCOGRAPHY, provider.supports_discography),
        (CAPABILITY_PLAYLIST_TRACKS_RESTRICTED, provider.restricts_playlist_tracks),
    )
    return [key for key, supports in declared if supports()]


def personal_capabilities(provider: BaseProvider) -> list[str]:
    """The capability keys of ``provider`` that act on a USER's own account of
    it (what connecting one enables), in a stable order. Declared by the
    provider with its ``supports_*`` methods, never inferred; the roles it
    fills are the ones its manifest names (``provider.roles``)."""
    keys: list[str] = []
    if provider.auth.account_model.has_personal and provider.supports_login():
        keys.append(CAPABILITY_LOGIN)
    if "metadata" in provider.roles:
        keys += [key for key, supports in (
            (CAPABILITY_USER_LIBRARY, provider.supports_user_library),
            (CAPABILITY_UNLIKE_SAVED_TRACK, provider.supports_unlike_saved_track),
        ) if supports()]
    if "sync" in provider.roles and provider.supports_likes():
        keys.append(CAPABILITY_PROPAGATE_FAVORITES)
    if "sync" in provider.roles and provider.supports_playlists():
        keys.append(CAPABILITY_PROPAGATE_PLAYLISTS)
    return keys


# ------------------------------------------------------------------
# Registry protocol
# ------------------------------------------------------------------


@runtime_checkable
class ProviderRegistry(Protocol):
    """Interfaccia che il core usa per accedere ai provider registrati."""

    def get_metadata_provider(self, name: str) -> MetadataProvider | None: ...
    def get_download_provider(self, name: str) -> DownloadProvider | None: ...
    def get_storage_provider(self, name: str) -> StorageProvider | None: ...
    def get_sync_provider(self, name: str) -> SyncProvider | None: ...
    def get_identification_provider(self, name: str) -> IdentificationProvider | None: ...
    def all_metadata(self) -> list[MetadataProvider]: ...
    def all_download(self) -> list[DownloadProvider]: ...
    def all_lyrics(self) -> list[LyricsProvider]: ...
    def all_storage(self) -> list[StorageProvider]: ...
    def all_sync(self) -> list[SyncProvider]: ...
    def all_identification(self) -> list[IdentificationProvider]: ...
    def metadata_names(self) -> list[str]: ...
    def download_names(self) -> list[str]: ...
    def storage_names(self) -> list[str]: ...
    def sync_names(self) -> list[str]: ...
    def names_for_category(self, category: str) -> list[str]: ...
