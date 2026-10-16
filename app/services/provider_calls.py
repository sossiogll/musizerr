"""
Una chiamata a un provider che fallisce non è un errore dell'host.

Le rotte di dettaglio (una traccia, un album, ...) avvolgono la chiamata al
loro provider in :func:`provider_call`: qualunque eccezione del provider (un
502 dell'API remota, un timeout, un errore di parsing) diventa
:class:`ProviderCallError`, che l'handler registrato da
``routes/errors.py`` trasforma in una risposta documentata (404, 502 o 503
con ``error_code``), mai in un 500.

Le rotte che aggregano più provider non sollevano: raccolgono i fallimenti in
un ``core.provider_failures.ProviderFailures`` e li restituiscono come
``provider_errors`` (``services.errors.provider_errors``).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException

from services.errors import classify_error, http_status_for

logger = logging.getLogger(__name__)


class ProviderCallError(Exception):
    """``kind``, when given, is what the host established about the failure
    (e.g. "not_found" once the storage confirms the file is gone) and wins
    over the classification of the raw error."""

    def __init__(
        self, provider: str, error: BaseException, subject: str = "metadata_provider", kind: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {error}")
        self.provider = provider
        self.error = error
        self.subject = subject
        self._kind = kind

    @property
    def kind(self) -> str:
        return self._kind or classify_error(self.error)

    @property
    def status_code(self) -> int:
        return http_status_for(self.kind)


@contextmanager
def provider_call(provider: str, subject: str = "metadata_provider") -> Iterator[None]:
    """Avvolge SOLO le chiamate al provider: un errore dell'host (DB, bug)
    dentro il blocco verrebbe scambiato per un guasto del provider."""
    try:
        yield
    except (HTTPException, ProviderCallError):
        raise
    except Exception as exc:
        logger.warning("Provider %s failed: %s", provider, exc, exc_info=True)
        raise ProviderCallError(provider, exc, subject) from exc
