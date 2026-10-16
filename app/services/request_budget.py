"""
Il tempo che una richiesta concede ai suoi passi secondari.

Dopo la lettura principale (la ricerca, la traccia, l'album, ...) una rotta
arricchisce la risposta interrogando altri provider: gli ID e le copertine
degli altri provider di metadati, la disponibilità nel sync provider. Sono
passi utili ma non necessari: un provider lento o irraggiungibile non deve
tenere ferma la risposta.

:class:`RequestBudget` li limita per richiesta:

- una scadenza unica: ogni chiamata attende al più il tempo che resta, poi
  la risposta va avanti senza (il thread della chiamata viene abbandonato e
  finisce per conto suo, come i provider lenti della ricerca);
- un interruttore per provider: un provider che in questa richiesta ha già
  fallito o superato il tempo non viene più interrogato;
- i provider saltati e i fallimenti finiscono in ``failures``, che la rotta
  restituisce come ``provider_errors``.

Solo le chiamate ai provider passano dal budget: l'autenticazione (che
legge e scrive le credenziali nel DB) resta nel thread della richiesta.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Callable, TypeVar

from core.provider_failures import ProviderFailures
from services.errors import classify_error

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Esiti che non dicono nulla sulla salute del provider: non lo escludono dal
# resto della richiesta e non sono un fallimento da riportare.
_NOT_A_FAULT = ("not_configured", "not_found")


class ProviderSkipped(Exception):
    """Il provider non è stato interrogato (o non ha risposto in tempo): il
    motivo è già in ``RequestBudget.failures``."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} skipped: no time left in this request's enrichment budget or already failing")
        self.provider = provider


class RequestBudget:
    def __init__(
        self,
        seconds: float,
        failures: ProviderFailures | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = seconds
        self.failures = failures if failures is not None else ProviderFailures()
        self._clock = clock
        self._deadline = clock() + seconds
        self._tripped: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def usable(self, provider: str) -> bool:
        """C'è ancora tempo e il provider non ha già fallito in questa richiesta."""
        with self._lock:
            tripped = provider in self._tripped
        return not tripped and self.remaining() > 0

    def skips(self, provider: str, subject: str | None = None) -> bool:
        """True se il provider va saltato: ha già fallito in questa richiesta,
        o il tempo è finito (e allora è riportato come scaduto)."""
        with self._lock:
            if provider in self._tripped:
                return True
        if self.remaining() <= 0:
            self.expire(provider, subject)
            return True
        return False

    def fail(self, provider: str, error: BaseException | str, subject: str | None = None) -> None:
        """Il provider ha fallito: da ora è saltato, e il motivo è riportato.
        Un provider non configurato o che non ha la risorsa non ha fallito."""
        if classify_error(error) in _NOT_A_FAULT:
            return
        with self._lock:
            self._tripped.add(provider)
        self.failures.record(provider, error, subject)

    def expire(self, provider: str, subject: str | None = None) -> None:
        """Il provider non ha risposto entro la scadenza."""
        logger.warning("Enrichment budget %.1fs exceeded on %s; answering without it", self.seconds, provider)
        self.fail(provider, TimeoutError(f"no answer within the {self.seconds:.1f}s enrichment budget"), subject)

    # ------------------------------------------------------------------

    def call(self, provider: str, fn: Callable[..., T], *args, subject: str | None = None, **kwargs) -> T:
        """``fn(*args, **kwargs)``, una chiamata al provider ``provider``,
        attesa al più fino alla scadenza. Solleva :class:`ProviderSkipped` se
        il provider è saltato o non risponde in tempo; un'eccezione del
        provider viene registrata e rilanciata."""
        if self.skips(provider, subject):
            raise ProviderSkipped(provider)
        future = self._submit(provider, fn, *args, **kwargs)
        try:
            return future.result(timeout=self.remaining())
        except FuturesTimeoutError:
            future.cancel()
            self.expire(provider, subject)
            raise ProviderSkipped(provider) from None
        except Exception as exc:
            self.fail(provider, exc, subject)
            raise

    @staticmethod
    def _submit(provider: str, fn: Callable[..., T], *args, **kwargs) -> Future:
        # Un esecutore per chiamata, chiuso subito senza attendere: una
        # chiamata appesa occupa solo il proprio thread, mai quelli di altre
        # richieste.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"enrich-{provider}")
        try:
            return executor.submit(fn, *args, **kwargs)
        finally:
            executor.shutdown(wait=False)


def bounded_call(budget: RequestBudget | None, provider: str, fn: Callable[..., T], *args,
                 subject: str | None = None) -> T:
    """Una chiamata al provider: entro ``budget``, se c'è; altrimenti diretta
    (i worker, che non hanno una risposta da restituire in tempo)."""
    if budget is None:
        return fn(*args)
    return budget.call(provider, fn, *args, subject=subject)


def skip_provider(budget: RequestBudget | None, provider: str, subject: str | None = None) -> bool:
    """True se ``budget`` dice di saltare il provider (vedi ``RequestBudget.skips``)."""
    return budget is not None and budget.skips(provider, subject)


def enrichment_budget(config, failures: ProviderFailures | None = None) -> RequestBudget:
    """Il budget dei passi secondari di una risposta, da ora
    (``enrichment_budget_seconds``): si crea dopo la lettura principale."""
    return RequestBudget(config.enrichment_budget_seconds, failures)
