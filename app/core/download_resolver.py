from __future__ import annotations
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from musizeer_sdk.errors import DelegateHoldsRelease, DownloadDeclined, DownloadDelegated
from musizeer_sdk.identifiers import Identifier
from .interfaces import ProviderRegistry
from .matching import requested_track_in_package
from musizeer_sdk.models import Track
from .provider_cooldown import NoCooldown, ProviderCooldown
from .provider_slots import ProviderSlots, UnlimitedSlots
from .auth_manager import AuthManager, AuthError
from .utils import provider_sort_key

# Callback tipo: (provider_name: str, method: str) -> None
ProgressCallback = Callable[[str, str], None] | None

logger = logging.getLogger(__name__)

# The code of the failure of a track whose release is complete, without it, in the
# external system a download was delegated to.
ALREADY_IN_DELEGATE = "already_in_delegate"


class MatchConfidence(Enum):
    EXACT = "id_match"
    FUZZY = "fuzzy_match"
    FAILED = "no_match"


@dataclass
class ResolveResult:
    success: bool
    provider: str | None = None
    confidence: MatchConfidence = MatchConfidence.FAILED
    # Nome del file scaricato, relativo alla cartella di lavoro di ``provider`` (quella che
    # resolve() ha chiesto per lui con ``workspace_for``).
    file_name: str | None = None
    error: str | None = None
    delegated: bool = False
    delegated_details: list | None = None
    delegated_handle: str | None = None
    # True quando almeno un provider ha fallito con un errore *transitorio*
    # (eccezione a runtime: rete, 5xx, hang interrotto da timeout...). Se resta
    # False significa esito definitivo — nessun provider ha la traccia — e il
    # chiamante NON deve ritentare (fail-fast).
    retriable: bool = False
    # Why a provider declined the track (``DownloadDeclined.code``), when no
    # provider downloaded it and at least one declined.
    error_code: str | None = None
    # Secondi prima che un provider saltato perché in pausa (cool-down) torni
    # disponibile: il chiamante non ritenta prima (0 = nessuna pausa in gioco).
    retry_after: float = 0.0


class DownloadResolver:
    """
    Dato un Track già identificato, trova il provider di download
    più adatto e avvia il download.

    Fase 1 — ID diretto: ogni provider prova OGNI identificativo della
              Track che dichiara di supportare (una traccia può avere più id
              dello stesso provider: il primo può non essere più scaricabile).
              Ordine: ISRC prima (più universale), poi ID provider-specific.

    Fase 2 — Fuzzy: i provider non provati per ID che dichiarano
              ``supports_fuzzy`` provano download_fuzzy().

    Un provider che il cool-down fa riposare (``ProviderCooldown``) è
    saltato: il download si ritenta più tardi invece di fallire.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        auth_manager: AuthManager,
        provider_order: list[str] | None = None,
        slots: ProviderSlots | None = None,
        cooldown: ProviderCooldown | None = None,
    ) -> None:
        self._registry = registry
        self._auth = auth_manager
        self._order: list[str] = provider_order or []
        self._slots: ProviderSlots = slots or UnlimitedSlots()
        self._cooldown: ProviderCooldown = cooldown or NoCooldown()

    def accepts_any(self, identifiers: list[Identifier]) -> bool:
        """True se almeno un provider di download sa scaricare per uno di
        questi identificativi: altrimenti la Phase 1 non ha nulla da provare."""
        return any(
            provider.supports_identifier(identifier)
            for provider in self._sorted_providers()
            for identifier in identifiers
        )

    def resolve(
        self,
        track: Track,
        workspace_for: Callable[[str], str],
        on_trying: ProgressCallback = None,
    ) -> ResolveResult:
        tried_by_id: set[str] = set()
        attempted: set[tuple[str, str]] = set()
        outcome = _Outcome()

        # --- Fase 1: match per ID diretto ---
        for identifier in self._sorted_identifiers(track.external_ids):
            for provider in self._sorted_providers():
                key = (provider.name, identifier.storage_type() + ":" + identifier.value)
                if key in attempted or not provider.supports_identifier(identifier):
                    continue
                attempted.add(key)
                tried_by_id.add(provider.name)
                if self._resting(provider, outcome):
                    continue
                logger.info(
                    "Trying %s for track '%s' via %s=%s",
                    provider.name, track.title,
                    identifier.storage_type(), identifier.value,
                )
                if on_trying:
                    on_trying(provider.name, "id")

                result = self._attempt(
                    provider, MatchConfidence.EXACT, outcome,
                    lambda session, p=provider, i=identifier: p.download_by_id(i, workspace_for(p.name), session),
                )
                if result is not None:
                    return result

        # --- Fase 2: fuzzy ---
        for provider in self._sorted_providers():
            if provider.name in tried_by_id or not provider.supports_fuzzy:
                continue
            if self._resting(provider, outcome):
                continue
            logger.info("Trying fuzzy on %s for track '%s'", provider.name, track.title)
            if on_trying:
                on_trying(provider.name, "fuzzy")

            result = self._attempt(
                provider, MatchConfidence.FUZZY, outcome,
                lambda session, p=provider: download_fuzzy(p, track, workspace_for(p.name), session),
            )
            if result is not None:
                return result

        if outcome.declined is not None:
            return ResolveResult(
                success=False,
                error=str(outcome.declined),
                error_code=outcome.declined.code,
                retriable=outcome.transient,
                retry_after=outcome.retry_after,
            )
        return ResolveResult(
            success=False,
            error=f"No provider could download '{track.title}' by '{track.artist}'",
            retriable=outcome.transient,
            retry_after=outcome.retry_after,
        )

    def _resting(self, provider, outcome: "_Outcome") -> bool:
        """Il provider riposa (cool-down): saltato, e il download va
        ritentato più tardi."""
        if not self._cooldown.cooling(provider.name):
            return False
        logger.warning("%s is cooling down after repeated errors: skipped", provider.name)
        outcome.transient = True
        outcome.retry_after = max(outcome.retry_after, self._cooldown.remaining_seconds(provider.name))
        return True

    def _attempt(
        self,
        provider,
        confidence: MatchConfidence,
        outcome: "_Outcome",
        download: Callable[[object], str | None],
    ) -> ResolveResult | None:
        """Un tentativo su ``provider``, nel rispetto del suo limite di
        concorrenza. Ritorna il risultato definitivo, o None per passare al
        tentativo successivo (in ``outcome`` un errore ritentabile o un
        rifiuto motivato). Un posto non ottenuto entro l'attesa vale come
        "questo provider è occupato": si passa al successivo e, se nessuno
        scarica, il download si ritenta più tardi invece di fallire."""
        limit = provider.max_concurrent_downloads
        slot = self._slots.acquire(provider.name, limit) if limit else nullcontext(True)
        try:
            with slot as acquired:
                if not acquired:
                    logger.warning("No free download slot on %s (limit %d)", provider.name, limit)
                    outcome.transient = True
                    return None
                session = self._auth.authenticate(provider)
                file_name = download(session)
        except DownloadDelegated as exc:
            self._cooldown.record(provider.name, ok=True)
            return ResolveResult(
                success=False,
                delegated=True,
                provider=exc.provider,
                delegated_details=exc.details,
                delegated_handle=exc.handle,
            )
        except DownloadDeclined as exc:
            self._cooldown.record(provider.name, ok=True)
            logger.info("%s", exc)
            outcome.declined = outcome.declined or exc
            return None
        except AuthError as exc:
            logger.warning("Auth failed for %s: %s", provider.name, exc)
            return None
        except Exception as exc:
            logger.warning("Download failed on %s: %s", provider.name, exc)
            self._cooldown.record(provider.name, ok=False)
            outcome.transient = True
            return None
        self._cooldown.record(provider.name, ok=True)
        if not file_name:
            return None
        return ResolveResult(
            success=True,
            provider=provider.name,
            confidence=confidence,
            file_name=file_name,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sorted_providers(self):
        all_providers = self._registry.all_download()
        return sorted(
            all_providers,
            key=lambda p: provider_sort_key(p.name, self._order),
        )

    @staticmethod
    def _sorted_identifiers(identifiers: list[Identifier]) -> list[Identifier]:
        """
        ISRC e MBID prima (universali), poi gli ID provider-specific.
        Dentro ogni gruppo l'ordine originale è mantenuto.
        """
        universal = [i for i in identifiers if i.is_universal]
        specific = [i for i in identifiers if not i.is_universal]
        return universal + specific


def download_fuzzy(provider, track: Track, workspace: str, session) -> str | None:
    """``provider.download_fuzzy``, with the host deciding what a delegate that
    ALREADY holds the track's release means (``DelegateHoldsRelease``: the
    provider reports what it holds, as data). The track has a file there:
    delegated, nothing to search (the first poll lands it). The release is
    complete without it: declined (``already_in_delegate``), no search would
    bring it. Otherwise the delegate is asked to look for the release
    (``search_delegated``, which raises ``DownloadDelegated`` on success)."""
    try:
        return provider.download_fuzzy(track, workspace, session)
    except DelegateHoldsRelease as held:
        if requested_track_in_package(track, held.tracks):
            logger.info("%s: '%s' already has a file in '%s'", provider.name, track.title, held.release)
            raise DownloadDelegated(provider.name, held.details, held.handle) from None
        if held.complete:
            raise DownloadDeclined(
                provider.name, ALREADY_IN_DELEGATE,
                f"album '{held.release}' is already complete in the external library without '{track.title}'",
            ) from None
        provider.search_delegated(held, session)
        return None


@dataclass
class _Outcome:
    """Ciò che i tentativi falliti di una risoluzione lasciano: un errore
    ritentabile (``transient``) e il primo rifiuto motivato."""

    transient: bool = False
    declined: DownloadDeclined | None = None
    retry_after: float = 0.0
