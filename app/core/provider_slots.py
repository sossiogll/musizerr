"""
Posti di esecuzione per i provider che dichiarano un limite di concorrenza
(``DownloadProvider.max_concurrent_downloads``). Il resolver chiede un posto
prima di chiamare il provider; l'host decide come contarli fra i processi.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Protocol


class ProviderSlots(Protocol):
    def acquire(self, provider_name: str, limit: int) -> AbstractContextManager[bool]:
        """Context manager che produce True se ha ottenuto uno dei ``limit``
        posti di ``provider_name`` entro l'attesa prevista (e lo rilascia
        all'uscita), False se l'attesa è scaduta."""
        ...


class UnlimitedSlots:
    """Nessun coordinamento: ogni richiesta ottiene subito un posto."""

    def acquire(self, provider_name: str, limit: int) -> AbstractContextManager[bool]:
        return nullcontext(True)
