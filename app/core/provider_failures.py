"""
Fallimenti dei provider durante un'operazione che ne interroga più d'uno
(ricerca, discover, tracce correlate, ...). Chi aggrega non si ferma al primo
provider che fallisce: restituisce ciò che è riuscito e annota qui chi è
mancato, così la risposta può dire che i risultati sono incompleti.

Solo dati: la classificazione dell'errore e il testo mostrato all'utente sono
politica dell'host (``services/errors.py``).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    # L'eccezione sollevata dal provider (o dall'autenticazione), oppure un
    # testo quando non c'è un'eccezione (es. budget di tempo scaduto).
    error: BaseException | str
    # Il tipo di provider che ha fallito (es. "sync_provider"), quando non è
    # quello dell'operazione; None = quello dell'operazione.
    subject: str | None = None


class ProviderFailures:
    """Raccolta thread-safe: i fan-out chiamano i provider in parallelo.
    Un provider compare una volta sola (il primo errore resta)."""

    def __init__(self) -> None:
        self._items: dict[str, ProviderFailure] = {}
        self._lock = threading.Lock()

    def record(self, provider: str, error: BaseException | str, subject: str | None = None) -> None:
        with self._lock:
            self._items.setdefault(provider, ProviderFailure(provider, error, subject))

    def extend(self, other: "ProviderFailures") -> None:
        for failure in other:
            self.record(failure.provider, failure.error, failure.subject)

    def __iter__(self) -> Iterator[ProviderFailure]:
        with self._lock:
            return iter(list(self._items.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, provider: object) -> bool:
        with self._lock:
            return provider in self._items
