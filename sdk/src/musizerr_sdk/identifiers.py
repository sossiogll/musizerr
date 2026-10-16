from __future__ import annotations

from dataclasses import dataclass

# Costanti per tipi di identificativo universale
ISRC = "isrc"
MBID = "mbid"
PROVIDER_ID = "provider_id"
UNIVERSAL_IDENTIFIER_TYPES = frozenset({ISRC, MBID})

# The storage type of the id a sync provider gives a file of its library is
# ``<sync provider>_sync_id``: it names a file, not an item of the catalog.
SYNC_ID_SUFFIX = "_sync_id"


def is_sync_id_type(storage_type: str) -> bool:
    """Whether an identifier storage type is the sync id of a sync provider
    (``<sync provider>_sync_id``)."""
    return storage_type.endswith(SYNC_ID_SUFFIX)


@dataclass(frozen=True)
class Identifier:
    """Identificativo singolo, data-driven e provider-agnostico.

    Per ID universali:  ``Identifier(type="isrc", value="GBAYE9200715")``
    Per ID provider:    ``Identifier(type="provider_id", value="abc", provider="catalog")``
    """

    type: str
    value: str
    provider: str | None = None

    def to_dict(self) -> dict:
        data = {"type": self.storage_type(), "value": self.value}
        if self.provider:
            data["provider"] = self.provider
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Identifier:
        identifier_type = data["type"]
        # Converti il formato legacy "catalog_id" → (provider_id, provider="catalog");
        # il sync id ("<sync provider>_sync_id") resta com'è: non è l'id di un provider.
        if identifier_type.endswith("_id") and identifier_type != PROVIDER_ID and not is_sync_id_type(identifier_type):
            provider = identifier_type.removesuffix("_id")
            return cls(type=PROVIDER_ID, value=data["value"], provider=provider)
        return cls(
            type=identifier_type,
            value=data["value"],
            provider=data.get("provider"),
        )

    def storage_type(self) -> str:
        """Tipo come stringa per il DB (backward-compatible: 'catalog_id')."""
        if self.type == PROVIDER_ID and self.provider:
            return f"{self.provider}_id"
        return self.type

    @property
    def is_universal(self) -> bool:
        return self.type in UNIVERSAL_IDENTIFIER_TYPES
