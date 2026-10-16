from __future__ import annotations
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Query

from core.auth_manager import ProviderNotConfigured
from core.matching import match_album, match_artist, match_track
from core.provider_failures import ProviderFailures
from core.merge import enrich
from core.serialization import serialize_album, serialize_artist, serialize_track
from core.discography import ReleaseGroup
from core.utils import provider_sort_key
from services.library_coverage import release_coverage
from services.enrichment import enrich_dicts_with_stream_url
from services.track_state import annotate_tracks
from services.errors import provider_errors
from .deps import AuthManagerDep, ConfigDep, RegistryDep, SessionDep, ViewerDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discover")

# Voce di cache: (scadenza, elementi, fallimenti). Un risultato incompleto
# (qualche provider ha fallito) resta in cache poco, per riprovare presto il
# provider mancante senza martellarlo a ogni richiesta.
_cache: dict[str, tuple[float, list, ProviderFailures]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600
_INCOMPLETE_CACHE_TTL = 60


def _cache_get(key: str) -> tuple[list, ProviderFailures] | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1], entry[2]
        if entry:
            del _cache[key]
        return None


def _cache_set(key: str, value: list, failures: ProviderFailures) -> None:
    ttl = _INCOMPLETE_CACHE_TTL if len(failures) else _CACHE_TTL
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value, failures)


def _sorted_providers(registry, priority):
    return sorted(
        registry.all_metadata(),
        key=lambda p: provider_sort_key(p.name, priority),
    )


def _fetch_parallel(providers, method, auth_manager, limit, failures: ProviderFailures):
    results: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=max(len(providers), 1)) as executor:
        futures = {}
        for p in providers:
            try:
                session = auth_manager.authenticate(p)
            except ProviderNotConfigured:
                continue
            except Exception as exc:
                logger.warning("Auth failed for %s, skipping discover: %s", p.name, exc)
                failures.record(p.name, exc)
                continue
            futures[executor.submit(getattr(p, method), session, limit)] = p.name
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                logger.warning("Discover %s failed for %s: %s", method, name, exc)
                failures.record(name, exc)
                results[name] = []
    return results


def _cached(key: str, method: str, merge, registry, auth_manager, config, limit) -> tuple[list, ProviderFailures]:
    cached = _cache_get(key)
    if cached is None:
        failures = ProviderFailures()
        providers = _sorted_providers(registry, config.metadata_provider_priority)
        results = _fetch_parallel(providers, method, auth_manager, limit, failures)
        cached = (merge(results, config.metadata_provider_priority), failures)
        _cache_set(key, *cached)
    return cached


def _merge_albums(results_by_provider, priority):
    merged: list = []
    for name in sorted(results_by_provider, key=lambda n: provider_sort_key(n, priority)):
        for album in results_by_provider.get(name, []):
            existing = next((a for a in merged if match_album(a, album).matched), None)
            if existing:
                enrich(existing, album)
            else:
                if album.provider and album.provider not in album.providers:
                    album.providers.append(album.provider)
                merged.append(album)
    return merged


def _merge_tracks(results_by_provider, priority):
    merged: list = []
    for name in sorted(results_by_provider, key=lambda n: provider_sort_key(n, priority)):
        for track in results_by_provider.get(name, []):
            existing = next((t for t in merged if match_track(t, track).matched), None)
            if existing:
                enrich(existing, track)
            else:
                if track.provider and track.provider not in track.providers:
                    track.providers.append(track.provider)
                merged.append(track)
    return merged


def _merge_artists(results_by_provider, priority):
    merged: list = []
    for name in sorted(results_by_provider, key=lambda n: provider_sort_key(n, priority)):
        for artist in results_by_provider.get(name, []):
            existing = next((a for a in merged if match_artist(a, artist).matched), None)
            if existing:
                enrich(existing, artist)
            else:
                if artist.provider and artist.provider not in artist.providers:
                    artist.providers.append(artist.provider)
                merged.append(artist)
    return merged


def _albums_with_coverage(albums: list, session, registry) -> list[dict]:
    """Le schede album della Home, ognuna con la sua copertura della
    libreria (per-richiesta, mai in cache: lo stato libreria cambia), la
    stessa della discografia di un artista."""
    coverages = release_coverage([ReleaseGroup(representative=a, editions=[a]) for a in albums], session, registry)
    return [{**serialize_album(a), "library": c} for a, c in zip(albums, coverages)]


@router.get("/new-releases")
def new_releases(
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    config: ConfigDep,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
):
    cached, failures = _cached("new-releases", "get_new_releases", _merge_albums, registry, auth_manager, config, limit)
    return {"items": _albums_with_coverage(cached[:limit], session, registry), "provider_errors": provider_errors(failures)}


@router.get("/trending/albums")
def trending_albums(
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    config: ConfigDep,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
):
    cached, failures = _cached("trending-albums", "get_trending_albums", _merge_albums, registry, auth_manager, config, limit)
    return {"items": _albums_with_coverage(cached[:limit], session, registry), "provider_errors": provider_errors(failures)}


@router.get("/trending/tracks")
def trending_tracks(
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    config: ConfigDep,
    session: SessionDep,
    viewer: ViewerDep,
    limit: int = Query(20, ge=1, le=100),
):
    cached, failures = _cached("trending-tracks", "get_trending_tracks", _merge_tracks, registry, auth_manager, config, limit)
    # Marca (per-richiesta, non in cache) i brani già presenti in libreria con
    # lo stream_url del sync provider: la UI li mostra come scaricati e li rende
    # riproducibili via stream. Opera sui dict serializzati per non mutare gli
    # oggetti in cache (lo stato libreria cambia nel tempo).
    items = [serialize_track(t) for t in cached[:limit]]
    enrich_dicts_with_stream_url(items, registry, auth_manager)
    annotate_tracks(items, session, viewer, registry)
    return {"items": items, "provider_errors": provider_errors(failures)}


@router.get("/trending/artists")
def trending_artists(
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    config: ConfigDep,
    limit: int = Query(20, ge=1, le=100),
):
    cached, failures = _cached("trending-artists", "get_trending_artists", _merge_artists, registry, auth_manager, config, limit)
    return {"items": [serialize_artist(a) for a in cached[:limit]], "provider_errors": provider_errors(failures)}
