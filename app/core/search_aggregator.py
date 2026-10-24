from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import TypeVar

from .interfaces import ProviderRegistry
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Album, AlbumRef, Artist, Playlist, Track
from .merge import enrich
from .matching import match_album, match_artist, match_track
from .auth_manager import AuthManager, ProviderNotConfigured
from .provider_failures import ProviderFailures
from .utils import provider_sort_key

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_LIMIT = 25

# Default del budget wall-clock per il fan-out (secondi). Il valore effettivo è
# configurabile dal catalogo (search_provider_budget_seconds), passato via deps;
# questa costante resta solo come default del costruttore. Serve quando un
# provider è patologicamente lento (rate-limit, refresh appeso): non blocca la
# ricerca, si restituisce ciò che ha risposto in tempo.
_SEARCH_BUDGET_S = 5.0

T = TypeVar("T", Track, Album, Artist)


class SearchAggregator:
    """
    Esegue la ricerca in parallelo su tutti i metadata provider abilitati,
    poi unisce i risultati per identificativi comuni (ISRC, MBID) e, dove
    non possibile, per nome+artista normalizzati.

    La priorità dei provider è configurabile: il provider con priorità
    più alta vince in caso di conflitto sui metadati (titolo, durata, ecc.).
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        auth_manager: AuthManager,
        provider_priority: list[str] | None = None,
        provider_search_limits: dict[str, int] | None = None,
        search_budget_seconds: float = _SEARCH_BUDGET_S,
        default_search_limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> None:
        self._registry = registry
        self._auth = auth_manager
        self._priority: list[str] = provider_priority or []
        self._provider_limits: dict[str, int] = provider_search_limits or {}
        # Configurabili (dal catalogo, via deps); i default = costanti modulo.
        self._budget = search_budget_seconds
        self._default_limit = default_search_limit

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    # ``failures``, se dato, raccoglie i provider che non hanno risposto (errore,
    # autenticazione fallita, budget di tempo scaduto): i risultati restituiti
    # sono quelli degli altri. Un provider non configurato non è un fallimento.

    def search_tracks(self, query: str, provider_filter: list[str] | None = None,
                      failures: ProviderFailures | None = None) -> list[Track]:
        results = self._parallel_search("search_tracks", query, provider_filter, failures)
        merged = self._merge_tracks(results)
        return self._sort_by_richness(merged)

    def search_albums(self, query: str, provider_filter: list[str] | None = None,
                      failures: ProviderFailures | None = None) -> list[Album]:
        results = self._parallel_search("search_albums", query, provider_filter, failures)
        merged = self._merge_items(results, matcher=match_album)
        return self._sort_by_richness(merged)

    def search_artists(self, query: str, provider_filter: list[str] | None = None,
                       failures: ProviderFailures | None = None) -> list[Artist]:
        results = self._parallel_search("search_artists", query, provider_filter, failures)
        merged = self._merge_items(results, matcher=match_artist)
        return self._sort_by_richness(merged)

    def search_playlists(self, query: str, provider_filter: list[str] | None = None,
                         failures: ProviderFailures | None = None) -> list[Playlist]:
        results = self._parallel_search("search_playlists", query, provider_filter, failures)
        return [item for items in results.values() for item in items]

    # ------------------------------------------------------------------
    # Ricerca parallela
    # ------------------------------------------------------------------

    def _parallel_search(
        self, method: str, query: str, provider_filter: list[str] | None = None,
        failures: ProviderFailures | None = None,
    ) -> dict[str, list]:
        failures = failures if failures is not None else ProviderFailures()
        providers = [
            p for p in self._sorted_providers()
            if p.implements(method) and (provider_filter is None or p.name in provider_filter)
        ]

        # Authenticate serially BEFORE fanning out. authenticate() reads/writes a
        # shared DB session (CredentialStore) that is NOT safe to touch from
        # multiple threads at once ("session is provisioning a new connection;
        # concurrent operations are not permitted") — that race was silently
        # dropping results from providers that need auth. The threads below then
        # run only the network-bound provider search, which is safe to parallelize.
        prepared: list[tuple[MetadataProvider, object]] = []
        for p in providers:
            try:
                session = self._auth.authenticate(p)
            except ProviderNotConfigured:
                continue
            except Exception as exc:
                logger.warning("Provider %s auth failed: %s", p.name, exc)
                failures.record(p.name, exc)
                continue
            prepared.append((p, session))

        results: dict[str, list] = {}
        executor = ThreadPoolExecutor(max_workers=len(prepared) or 1)
        futures = {
            executor.submit(self._safe_search, p, method, query, session): p.name
            for p, session in prepared
        }
        try:
            for future in as_completed(futures, timeout=self._budget):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:  # Provider errors are unpredictable (network, auth, parsing, etc.)
                    logger.warning("Provider %s search failed: %s", name, exc)
                    failures.record(name, exc)
                    results[name] = []
        except FuturesTimeoutError:
            slow = [name for fut, name in futures.items() if not fut.done()]
            for name in slow:
                failures.record(name, TimeoutError(f"no answer within the {self._budget:.1f}s search budget"))
            logger.warning(
                "Search budget %.1fs exceeded; returning partial results, dropped: %s",
                self._budget, slow,
            )
        finally:
            # Don't block the response on stragglers — abandon them and let their
            # pool threads finish in the background.
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    def _safe_search(self, provider: MetadataProvider, method: str, query: str, session) -> list:
        limit = self._provider_limits.get(provider.name, self._default_limit)
        return getattr(provider, method)(query, session, limit)

    # ------------------------------------------------------------------
    # Merge tracce
    # ------------------------------------------------------------------

    def _merge_tracks(self, results_by_provider: dict[str, list[Track]]) -> list[Track]:
        merged: list[Track] = []
        seen_internal: set[str] = set()

        for provider_name in self._sorted_provider_names(results_by_provider):
            for track in results_by_provider.get(provider_name, []):
                matches = self._find_matching_tracks(merged, track)
                if matches:
                    for existing in matches:
                        self._enrich(existing, track)
                else:
                    if track.internal_id not in seen_internal:
                        if track.album and not any(a.name == track.album for a in track.albums):
                            track.albums.append(AlbumRef(name=track.album, provider=track.provider, provider_id=track.album_provider_id))
                        if track.provider and track.provider not in track.providers:
                            track.providers.append(track.provider)
                        merged.append(track)
                        seen_internal.add(track.internal_id)

        return self._consolidate_tracks(merged)

    def _consolidate_tracks(self, merged: list[Track]) -> list[Track]:
        """
        Post-merge pass: entries enriched from different providers may now
        share ISRCs/MBIDs that weren't present during the initial merge,
        or may be featuring variants of the same track.
        Collapse any such duplicates.
        """
        i = 0
        while i < len(merged):
            j = i + 1
            while j < len(merged):
                if match_track(merged[i], merged[j]).matched:
                    self._enrich(merged[i], merged[j])
                    merged.pop(j)
                else:
                    j += 1
            i += 1
        return merged

    def _find_matching_tracks(self, merged: list[Track], candidate: Track) -> list[Track]:
        return [existing for existing in merged if match_track(existing, candidate).matched]

    # ------------------------------------------------------------------
    # Merge generico per Album e Artist
    # ------------------------------------------------------------------

    def _merge_items(self, results_by_provider: dict[str, list[T]], matcher) -> list[T]:
        merged: list[T] = []

        for provider_name in self._sorted_provider_names(results_by_provider):
            for item in results_by_provider.get(provider_name, []):
                existing = next((current for current in merged if matcher(current, item).matched), None)
                if existing:
                    self._enrich(existing, item)
                else:
                    if hasattr(item, "providers") and item.provider and item.provider not in item.providers:
                        item.providers.append(item.provider)
                    merged.append(item)

        return merged

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enrich(self, existing, candidate) -> None:
        enrich(existing, candidate)

    def _sort_by_richness(self, items: list[Track] | list[Album] | list[Artist]) -> list[Track] | list[Album] | list[Artist]:
        def sort_key(item: Track | Album | Artist) -> tuple[int, int, int]:
            n_ids = len(getattr(item, "external_ids", []))
            year = getattr(item, "year", None) or 0
            provider = getattr(item, "provider", "")
            priority = provider_sort_key(provider, self._priority)
            return (-n_ids, -year, priority)
        return sorted(items, key=sort_key)

    def _sorted_providers(self) -> list[MetadataProvider]:
        all_providers = self._registry.all_metadata()
        return sorted(
            all_providers,
            key=lambda p: provider_sort_key(p.name, self._priority),
        )

    def _sorted_provider_names(self, results: dict[str, list]) -> list[str]:
        names = list(results.keys())
        return sorted(
            names,
            key=lambda n: provider_sort_key(n, self._priority),
        )
