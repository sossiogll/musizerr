from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

from sqlmodel import select

from core.auth_manager import ProviderNotConfigured
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from core.discography import RELEASES_LISTING_LIMIT
from musizerr_sdk.models import Album, AlbumRef, Track
from core.interfaces import SyncAvailability
from core.matching import find_match, match_album, match_artist, match_artist_name, match_track, pair_album_tracks
from musizerr_sdk.text import normalize
from core.merge import enrich
from core.serialization import deserialize_track
from services.config_manager import get_config
from services import public_urls
from services.track_identity import sync_aliases, sync_provider_names
from services.request_budget import ProviderSkipped, RequestBudget, bounded_call as _call, skip_provider as _skip

logger = logging.getLogger(__name__)

SYNC_SUBJECT = "sync_provider"


def _implements_all(provider, *methods: str) -> bool:
    """Whether the provider's manifest says it implements every one of
    ``methods``. Asked BEFORE authenticating or calling it: a provider that
    cannot answer (a likes-only one has no search) is not asked, and its
    refusal to sign in a guest is not a failure of the enrichment (the same
    rule as ``core/search_aggregator.py``)."""
    return all(provider.implements(method) for method in methods)


def _can_find_track(provider, track) -> bool:
    """Whether ``_find_track_candidate`` has anything to ask ``provider``: an
    ISRC lookup it declares, or a search it implements."""
    return (bool(track.get_id(ISRC)) and provider.supports_isrc_lookup()) or provider.implements("search_tracks")

# Enrichment della disponibilità nel sync: risolvere lo stream URL significa una
# ricerca per risultato; serializzarla era il costo dominante della ricerca
# (~13s per ~50 risultati). Parallelizziamo e mettiamo in cache brevemente.
# Il numero di worker e il TTL della cache sono CONFIGURABILI (settings_catalog:
# search_stream_enrich_workers / sync_availability_cache_ttl_seconds), letti dal
# ConfigManager condiviso. _AVAIL_CACHE_MAX resta una costante: è un bound di
# memoria (limite di sicurezza dell'implementazione), non una policy utente.
_AVAIL_CACHE_MAX = 4096
_avail_cache: dict[tuple, tuple[float, object]] = {}
_avail_lock = threading.Lock()


def _avail_cache_get(key: tuple) -> tuple[bool, object]:
    now = time.monotonic()
    with _avail_lock:
        hit = _avail_cache.get(key)
        if hit and hit[0] > now:
            return True, hit[1]
    return False, None


def _avail_cache_put(key: tuple, value: object) -> None:
    ttl = get_config().sync_availability_cache_ttl_seconds
    with _avail_lock:
        if len(_avail_cache) > _AVAIL_CACHE_MAX:
            _avail_cache.clear()  # crude bound; the TTL keeps entries fresh anyway
        _avail_cache[key] = (time.monotonic() + ttl, value)


# ------------------------------------------------------------------
# Stream URL enrichment (sync provider lookup)
# ------------------------------------------------------------------


def check_sync_availability(sync, track: Track, session) -> SyncAvailability | None:
    """
    Disponibilità di una traccia nel sync provider. Il provider cerca nella
    propria API e restituisce i CANDIDATI con il loro sync id
    (``SyncProvider.find_candidates``); è QUI, in un punto solo, che l'host decide
    quale è la traccia (``find_match``: titolo + tutti gli artisti + anno, con la
    normalizzazione del core) e ne ricava il sync id. Nessuna logica di match o
    di pulizia duplicata nei provider.
    """
    candidates = sync.find_candidates(track, session)
    match = find_match(track, [candidate.track for candidate in candidates])
    if match is None:
        return None
    return SyncAvailability(sync_id=next(c.sync_id for c in candidates if c.track is match))


def _enrich_with_stream_url(items: list, registry, auth_manager, *, get_track, record_sync_hit,
                            budget: RequestBudget | None = None) -> None:
    """Set stream_url on items available in any sync provider (accessor ``get_track``,
    funziona su Track e dict). Un hit registra anche il sync provider via
    ``record_sync_hit`` (providers + external_ids), non solo lo stream_url.

    Decide by the identifiers an item carries, never by whether it already
    has some stream URL (a like imported from elsewhere keeps the URL of its
    provider): an item that carries a sync id of the sync provider needs no
    lookup, an item of the sync provider itself is a hit with its own id
    (one identity space, ``services.track_identity``), every other item is
    looked up.

    Con ``budget`` le ricerche nel sync provider si fermano alla scadenza
    della richiesta, e il primo errore del sync provider salta le altre:
    gli elementi non ancora controllati restano senza stream_url.
    """
    syncs = registry.all_sync()
    if not syncs:
        return
    for sync in syncs:
        if _skip(budget, sync.name, SYNC_SUBJECT):
            continue
        try:
            session = auth_manager.authenticate(sync)
        except Exception:
            continue

        # Split items into known ones (resolved inline) and pending lookups.
        pending: list[tuple] = []
        for item in items:
            track = get_track(item)
            if not track or not track.title:
                continue
            known = _known_sync_id(track, sync.name)
            if known:
                record_sync_hit(item, sync.name, known)
                continue
            key = _availability_key(sync.name, track)
            cached, value = _avail_cache_get(key)
            if cached:
                if value:
                    record_sync_hit(item, sync.name, value)
                continue
            pending.append((item, track, key))

        if not pending:
            continue

        def _resolve(entry: tuple, sync=sync, session=session):
            item, track, key = entry
            if budget is not None and not budget.usable(sync.name):
                return None
            try:
                avail = check_sync_availability(sync, track, session)
            except Exception as exc:
                logger.debug("stream_url check failed for '%s' on %s: %s", track.title, sync.name, exc)
                if budget is not None:
                    budget.fail(sync.name, exc, SYNC_SUBJECT)
                return None
            sync_id = avail.sync_id if avail else None
            _avail_cache_put(key, sync_id)
            return (item, sync_id)

        # Each check is an independent network search on the sync provider
        # (httpx, thread-safe); parallelize instead of blocking the whole search.
        # The results are applied here, in the request's thread: a check still
        # running when the budget ends is abandoned and changes nothing.
        workers = min(get_config().search_stream_enrich_workers, len(pending))
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = [executor.submit(_resolve, entry) for entry in pending]
        try:
            for future in as_completed(futures, timeout=budget.remaining() if budget is not None else None):
                result = future.result()
                if result and result[1]:
                    record_sync_hit(result[0], sync.name, result[1])
        except FuturesTimeoutError:
            budget.expire(sync.name, SYNC_SUBJECT)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _known_sync_id(track: Track, sync_name: str) -> str | None:
    """The sync id of ``track`` on the sync provider, when its identifiers
    already say it: a stored sync id, or its own id on that provider."""
    sync_type = sync_id_type(sync_name)
    for identifier in track.external_ids:
        if identifier.storage_type() == sync_type:
            return identifier.value
    alias = next(iter(sync_aliases(track.external_ids, {sync_name})), None)
    return alias.value if alias else None


def _availability_key(sync_name: str, track: Track) -> tuple:
    """What a cached availability answer is about: the full title (with its
    version), the artist, the album and the duration, so two versions of a
    song never share an answer."""
    return (
        sync_name, normalize(track.title or ""), normalize(track.artist or ""), normalize(track.album or ""),
        round((track.duration_ms or 0) / 1000),
    )


def _record_sync_hit_on_track(track: Track, sync_name: str, sync_id: str) -> None:
    """Registra un hit sync su una Track: stream_url + providers + external_ids."""
    track.stream_url = public_urls.provider_stream_url(sync_name, sync_id)
    if sync_name not in track.providers:
        track.providers.append(sync_name)
    track.add_id(Identifier(type=sync_id_type(sync_name), value=sync_id))


def _record_sync_hit_on_dict(item: dict, sync_name: str, sync_id: str) -> None:
    """Come sopra, sulla variante dict serializzata."""
    item["stream_url"] = public_urls.provider_stream_url(sync_name, sync_id)
    providers = item.setdefault("providers", [])
    if sync_name not in providers:
        providers.append(sync_name)
    identifier = Identifier(type=sync_id_type(sync_name), value=sync_id)
    external_ids = item.setdefault("external_ids", [])
    already = any(e.get("type") == identifier.type and e.get("value") == identifier.value for e in external_ids)
    if not already:
        external_ids.append(identifier.to_dict())


def enrich_tracks_with_stream_url(tracks: list[Track], registry, auth_manager, budget: RequestBudget | None = None) -> None:
    """Set stream_url on Track objects that are available in any sync provider."""
    _enrich_with_stream_url(
        tracks, registry, auth_manager,
        get_track=lambda t: t,
        record_sync_hit=_record_sync_hit_on_track,
        budget=budget,
    )


def enrich_dicts_with_stream_url(tracks: list[dict], registry, auth_manager, budget: RequestBudget | None = None) -> None:
    """Like enrich_tracks_with_stream_url but operates on serialized dicts."""
    _enrich_with_stream_url(
        tracks, registry, auth_manager,
        get_track=lambda d: deserialize_track(d),
        record_sync_hit=_record_sync_hit_on_dict,
        budget=budget,
    )


def cross_enrich_track(track, registry, auth_manager, budget: RequestBudget | None = None) -> None:
    """Arricchisce il track con ID degli altri provider via direct lookup o fuzzy match.
    Con ``budget`` ogni provider è interrogato entro la scadenza della richiesta."""
    # Assicura che l'album nativo sia nella lista albums (i track da get_album hanno albums=[])
    if (track.album and track.album_provider_id and
            not any(a.provider_id == track.album_provider_id for a in track.albums)):
        track.albums.append(AlbumRef(
            name=track.album,
            provider=track.provider,
            provider_id=track.album_provider_id,
        ))
    isrc = track.get_id(ISRC)
    source_provider = next((provider for provider in registry.all_metadata() if provider.name == track.provider), None)
    if (source_provider is not None and source_provider.implements("find_related_tracks")
            and not _skip(budget, source_provider.name)):
        try:
            session = auth_manager.authenticate(source_provider)
            related_tracks = _call(budget, source_provider.name, source_provider.find_related_tracks, track, session, 25)
            for related in related_tracks:
                enrich(track, related)
        except Exception as exc:
            logger.debug("Same-provider track enrich failed for %s: %s", source_provider.name, exc)

    for other in registry.all_metadata():
        if other.name == track.provider or not _can_find_track(other, track) or _skip(budget, other.name):
            continue
        try:
            session = auth_manager.authenticate(other)
            result = _call(budget, other.name, _find_track_candidate, track, other, session)

            if result and match_track(track, result).matched:
                enrich(track, result)
                if not track.album and result.album:
                    track.album = result.album
            elif result:
                logger.warning(
                    "Cross-enrich track skipped suspicious %s match for '%s' (ISRC=%s)",
                    other.name,
                    track.title,
                    isrc.value if isrc else "",
                )
        except Exception as exc:
            logger.debug("Cross-enrich track failed for %s: %s", other.name, exc)


def complete_universal_ids(tracks, registry, auth_manager, budget: RequestBudget | None = None) -> None:
    """Le righe di una lista che non portano un identificatore universale
    (ISRC/MBID: alcuni provider lo omettono negli elenchi e lo danno solo nel
    dettaglio della traccia) lo prendono dal dettaglio del loro provider,
    entro il budget della richiesta. Chiamarla solo sulle righe che il
    database non sa collocare (``services.track_state.known_track_ids``): è
    una chiamata per riga."""
    for track in tracks:
        if any(identifier.is_universal for identifier in track.external_ids) or not track.provider_id:
            continue
        provider = registry.get_metadata_provider(track.provider)
        if provider is None or not provider.implements("get_track") or _skip(budget, provider.name):
            continue
        try:
            session = auth_manager.authenticate(provider)
            full = _call(budget, provider.name, provider.get_track, track.provider_id, session)
        except Exception as exc:  # noqa: BLE001 — la riga resta com'era
            logger.debug("Universal ids of %s/%s not completed: %s", track.provider, track.provider_id, exc)
            continue
        for identifier in (full.external_ids if full else ()):
            if identifier.is_universal:
                track.add_id(identifier)


def _find_track_candidate(track, provider, session):
    isrc = track.get_id(ISRC)
    if isrc and provider.supports_isrc_lookup():
        result = provider.find_by_isrc(isrc.value, session)
        if result is not None:
            return result

    query_parts = [track.title, track.artist]
    if getattr(track, "album", None):
        query_parts.append(track.album)
    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return None

    results = provider.search_tracks(query, session, 10)
    return next((candidate for candidate in results if match_track(track, candidate).matched), None)


@dataclass
class EnrichmentOutcome:
    """Esito dell'arricchimento incrociato: ``complete`` è False quando un
    provider o una traccia non si sono potuti confrontare (errore, non
    semplice assenza di corrispondenza), con il motivo."""

    complete: bool = True
    reason: str | None = None

    def fail(self, reason: str) -> None:
        self.complete = False
        self.reason = self.reason or reason

    def as_dict(self) -> dict:
        return {"complete": self.complete, "reason": self.reason}


def cross_enrich_album(album, registry, auth_manager, budget: RequestBudget | None = None) -> EnrichmentOutcome:
    """Arricchisce l'album con ID e cover_url degli altri provider via match
    titolo+artista normalizzato, e le sue tracce con quelle dell'album
    trovato. Un provider o una traccia che falliscono non fermano gli altri.
    Con ``budget`` ogni provider è interrogato entro la scadenza della richiesta.

    An album of a sync provider lists what the library holds, not the
    release: its track count is never taken, and when it differs from the
    release's its tracks enrich nothing."""
    outcome = EnrichmentOutcome()
    sync_names = sync_provider_names(registry)
    for other in registry.all_metadata():
        if other.name == album.provider or not other.implements("search_albums"):
            continue
        if _skip(budget, other.name):
            outcome.fail("provider_error")
            continue
        try:
            session = auth_manager.authenticate(other)
            results = _call(budget, other.name, other.search_albums, f"{album.title} {album.artist}", session, 5)
        except Exception as exc:
            logger.warning("Cross-enrich album search failed on %s: %s", other.name, exc)
            outcome.fail("provider_error")
            continue
        from_sync = other.name in sync_names
        for result in results:
            result, detail = _with_release_year(album, result, other, session, outcome, budget)
            m = match_album(album, result)
            if m.matched:
                enrich(album, catalog_view(result, from_sync))
            if m.matched or m.reason == "track_count_mismatch":
                if album.tracks and not (from_sync and m.reason == "track_count_mismatch"):
                    _enrich_album_tracks(album, other, result.provider_id, session, outcome, budget, full_album=detail)
                logger.debug("Cross-enrich album: matched '%s' on %s", album.title, other.name)
                break
    return outcome


def _with_release_year(album, result, provider, session, outcome: EnrichmentOutcome, budget: RequestBudget | None = None):
    """A provider whose search lists albums without their year (the year is
    only in the detail) would never match: ``match_album`` asks for the year
    of both sides. When the candidate has the album's title and artist and
    only the year is missing, its detail gives it. Returns the candidate to
    match (the detail when it was fetched) and the detail, so the track
    enrichment does not ask for it twice."""
    if result.year is not None or album.year is None:
        return result, None
    if normalize(result.title or "") != normalize(album.title or "") or normalize(result.artist or "") != normalize(album.artist or ""):
        return result, None
    try:
        detail = _call(budget, provider.name, provider.get_album, result.provider_id, session)
    except Exception as exc:  # noqa: BLE001 — the candidate stays as the search gave it
        logger.warning("Release year of '%s' from %s not read: %s", result.title, provider.name, exc)
        outcome.fail("provider_error")
        return result, None
    if detail is None or detail.year is None:
        return result, detail
    return replace(result, year=detail.year), detail


def catalog_view(album: Album, from_sync: bool) -> Album:
    """The album as a catalog entry: from a sync provider, without its track
    count (the tracks the library holds, not the tracks of the release)."""
    return replace(album, track_count=None) if from_sync else album


def _enrich_album_tracks(album, provider, matched_album_id, session, outcome: EnrichmentOutcome,
                         budget: RequestBudget | None = None, full_album=None) -> None:
    """Fetch the full album from the matched provider (unless the caller
    already has it) and enrich each track with the one it pairs with
    (``core.matching.pair_album_tracks``: one to one, by title identity,
    position and duration)."""
    if full_album is None:
        try:
            full_album = _call(budget, provider.name, provider.get_album, matched_album_id, session)
        except Exception as exc:
            logger.warning("Track enrichment from %s album failed: %s", provider.name, exc)
            outcome.fail("provider_error")
            return
    if not full_album or not full_album.tracks:
        return
    for source_index, candidate_index in pair_album_tracks(album.tracks, full_album.tracks).items():
        track = album.tracks[source_index]
        try:
            enrich(track, full_album.tracks[candidate_index])
        except Exception as exc:
            logger.warning("Track enrichment of '%s' from %s failed: %s", track.title, provider.name, exc)
            outcome.fail("track_error")


def cross_enrich_artist(artist, registry, auth_manager, budget: RequestBudget | None = None) -> None:
    """Arricchisce l'artista con ID e cover_url degli altri provider via match nome normalizzato.
    Con ``budget`` ogni provider è interrogato entro la scadenza della richiesta."""
    for other in registry.all_metadata():
        if other.name == artist.provider or not other.implements("search_artists") or _skip(budget, other.name):
            continue
        try:
            session = auth_manager.authenticate(other)
            results = _call(budget, other.name, other.search_artists, artist.name, session, 5)
            for result in results:
                if match_artist(artist, result).matched:
                    enrich(artist, result)
                    break
        except Exception as exc:
            logger.debug("Cross-enrich artist failed for %s: %s", other.name, exc)


def fill_missing_artist(albums, artist_name: str, artist_provider_id: str | None = None) -> None:
    """Gli album della discografia di un artista sono suoi: alcuni provider
    li elencano senza artista, che serve al matching delle uscite, e senza
    l'id dell'artista sul loro provider (``artist_provider_id``: quello
    dell'artista della discografia, dello stesso provider)."""
    for album in albums:
        if not album.artist:
            album.artist = artist_name
        if artist_provider_id and not album.artist_provider_id:
            album.artist_provider_id = artist_provider_id


def cross_enrich_artist_albums(artist_name, primary_provider, albums, registry, auth_manager, *, failures=None,
                               budget: RequestBudget | None = None) -> list:
    """Arricchisce la discografia con album e ID degli altri provider via match fuzzy.
    Di ogni provider si leggono al più ``RELEASES_LISTING_LIMIT`` uscite.
    Con ``budget`` ogni provider è interrogato entro la scadenza della richiesta
    (i fallimenti vanno allora in ``budget.failures``)."""
    sync_names = sync_provider_names(registry)
    for other in registry.all_metadata():
        if (other.name == primary_provider or not _implements_all(other, "search_artists", "get_artist_albums")
                or _skip(budget, other.name)):
            continue
        try:
            session = auth_manager.authenticate(other)
            results = _call(budget, other.name, other.search_artists, artist_name, session, 5)
            matched = next((r for r in results if match_artist_name(artist_name, r).matched), None)
            if not matched:
                logger.info("cross_enrich_artist_albums: artist '%s' not found on %s", artist_name, other.name)
                continue
            other_albums = [
                catalog_view(other_album, other.name in sync_names)
                for other_album in _call(budget, other.name, other.get_artist_albums, matched.provider_id, session, RELEASES_LISTING_LIMIT)
            ]
            fill_missing_artist(other_albums, matched.name, matched.provider_id)
            logger.info("cross_enrich_artist_albums: %s → %d albums from %s", artist_name, len(other_albums), other.name)
            added = 0
            for other_album in other_albums:
                existing = next((current for current in albums if match_album(current, other_album).matched), None)
                if existing is not None:
                    enrich(existing, other_album)
                else:
                    albums.append(other_album)
                    added += 1
            logger.info("cross_enrich_artist_albums: %d new albums added from %s", added, other.name)
        except (ProviderNotConfigured, ProviderSkipped):
            continue
        except Exception as exc:
            logger.warning("Cross-enrich artist albums failed for %s: %s", other.name, exc, exc_info=True)
            if failures is not None:
                failures.record(other.name, exc)

    return albums
