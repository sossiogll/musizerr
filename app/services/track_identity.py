"""
The identity space of a sync provider, as the host knows it.

A provider registered as a sync provider may also be a metadata provider:
its tracks then carry its provider id (``<name>_id``), and the library
records it indexed carry the same value as a sync id (``<name>_sync_id``,
``core.identifiers.sync_id_type``). They are one identity space: the like
imported from it and the library record of the same file are one record.
Only the registry knows which providers are sync providers, so the alias is
added here, by the host, never by a provider or by the core.
"""
from __future__ import annotations

from core.identifiers import sync_id_type
from musizeer_sdk.identifiers import PROVIDER_ID, Identifier
from musizeer_sdk.models import Track


def sync_provider_names(registry) -> set[str]:
    return {sync.name for sync in registry.all_sync()}


def sync_aliases(identifiers, sync_names: set[str]) -> list[Identifier]:
    """The sync ids that the provider ids among ``identifiers`` stand for:
    one for each provider id of a sync provider."""
    return [
        Identifier(type=sync_id_type(identifier.provider), value=identifier.value)
        for identifier in identifiers
        if identifier.type == PROVIDER_ID and identifier.provider in sync_names
    ]


def with_sync_aliases(track: Track, registry) -> Track:
    """``track`` with the sync id of each of its provider ids that belongs to
    a sync provider (in place; returned for convenience)."""
    for alias in sync_aliases(list(track.external_ids), sync_provider_names(registry)):
        track.add_id(alias)
    return track


def alias_pairs(pairs: set[tuple[str, str]], sync_names: set[str]) -> set[tuple[str, str]]:
    """``(storage type, value)`` pairs plus the sync id each provider id of a
    sync provider stands for."""
    aliases = {(sync_id_type(name), value) for name in sync_names for id_type, value in pairs if id_type == f"{name}_id"}
    return pairs | aliases
