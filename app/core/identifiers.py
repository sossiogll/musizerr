"""
How the host reads identifiers: which record a track is from the identifiers
it shares. ``Identifier`` itself (and the types it names) is part of the
provider contract: ``musizerr_sdk.identifiers``.
"""
from __future__ import annotations

from musizerr_sdk.identifiers import SYNC_ID_SUFFIX, UNIVERSAL_IDENTIFIER_TYPES, is_sync_id_type


def sync_id_type(sync_provider_name: str) -> str:
    """Id_type di ``track_identifiers`` per il sync id di un sync provider.

    ATTENZIONE: per un nome produce "<nome>_sync_id", byte-identico al
    letterale hardcoded usato dalle righe reali già in tabella — non alterare
    senza una migrazione dati.
    """
    return f"{sync_provider_name}{SYNC_ID_SUFFIX}"


def identity_rank(storage_type: str) -> int:
    """How much an identifier says about which recording a track is: a
    universal id (ISRC/MBID) 0, a provider id 1, a sync id 2 (it names a
    file of the library and changes when the library is rescanned)."""
    if storage_type in UNIVERSAL_IDENTIFIER_TYPES:
        return 0
    return 2 if is_sync_id_type(storage_type) else 1


AMBIGUOUS = "ambiguous"


def resolve_identity(hits) -> str | None:
    """The record a track is, from the stored identifiers it shares:
    ``hits`` are ``(storage type, record id)``. The best rank that has hits
    decides (``identity_rank``); when its hits point to more than one
    record the answer is ``AMBIGUOUS``, never an arbitrary one of them.
    ``None`` when nothing is shared. The same answer whatever the order of
    ``hits``."""
    by_rank: dict[int, set[str]] = {}
    for storage_type, record_id in hits:
        by_rank.setdefault(identity_rank(storage_type), set()).add(record_id)
    if not by_rank:
        return None
    records = by_rank[min(by_rank)]
    return next(iter(records)) if len(records) == 1 else AMBIGUOUS
