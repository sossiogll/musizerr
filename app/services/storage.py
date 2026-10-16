"""
Risoluzione del provider di storage attivo.

Nessun provider ha un default: se ``storage_provider`` non è impostato, o il
provider impostato non è (più) registrato, non c'è nessuno storage, e i
chiamanti gestiscono il "non c'è" con il loro 404/503/percorso di errore
(Golden Rule, CLAUDE.md: l'host non conosce nessun provider).
"""
from __future__ import annotations


def resolve_storage_provider(registry, configured_name: str | None):
    """Risolve lo StorageProvider attivo: quello di ``configured_name``
    (tipicamente ``config.storage_provider``), se impostato e registrato.

    Ritorna ``(storage, configured_name)`` — ``storage`` è ``None`` se il
    setting è vuoto o il provider non è registrato (e allora il nome è quello
    configurato, anche stantio, o ``None``: chi lo scrive da qualche parte
    lo fa solo dopo aver controllato che ``storage`` esista)."""
    storage = registry.get_storage_provider(configured_name) if configured_name else None
    return storage, configured_name
