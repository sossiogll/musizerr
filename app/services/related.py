"""
Contenuti collegati a una traccia, raccolti da tutti i provider di metadati
in ordine di priorità: le sue varianti correlate (``find_related_tracks``)
seguite dalle tracce simili (``find_similar_tracks``), e le uscite che
contengono la stessa registrazione (stesso ISRC o MBID).
Un provider che fallisce viene saltato.
"""
from __future__ import annotations

import logging

from musizerr_sdk.identifiers import ISRC, MBID
from core.matching import match_track, shared_track_identity
from musizerr_sdk.text import normalize
from core.auth_manager import ProviderNotConfigured
from musizerr_sdk.models import Track
from core.provider_failures import ProviderFailures
from core.utils import provider_sort_key
from services.request_budget import ProviderSkipped, RequestBudget, bounded_call, skip_provider

logger = logging.getLogger(__name__)


def _providers_by_priority(registry, priority: list[str]) -> list:
    return sorted(registry.all_metadata(), key=lambda p: provider_sort_key(p.name, priority))


def _is_same_item(a: Track, b: Track) -> bool:
    return a.provider == b.provider and a.provider_id == b.provider_id


def related_tracks(
    track: Track, registry, auth_manager, priority: list[str], limit: int,
    *, failures: ProviderFailures | None = None, budget: RequestBudget | None = None,
) -> list[Track]:
    """Prima le varianti della stessa traccia (``find_related_tracks``), poi
    le tracce simili (``find_similar_tracks``), ciascun gruppo da ogni
    provider in ordine di priorità. Deduplicate con ``match_track`` (la prima
    trovata resta), senza la traccia stessa. Con ``budget`` ogni provider è
    interrogato entro la scadenza della richiesta (i mancanti vanno in
    ``budget.failures``)."""
    found: list[Track] = []
    for method in ("find_related_tracks", "find_similar_tracks"):
        for provider in _providers_by_priority(registry, priority):
            if len(found) >= limit:
                return found[:limit]
            if skip_provider(budget, provider.name):
                continue
            try:
                session = auth_manager.authenticate(provider)
                candidates = bounded_call(budget, provider.name, getattr(provider, method), track, session, limit)
            except (ProviderNotConfigured, ProviderSkipped):
                continue
            except Exception as exc:
                logger.warning("%s failed on %s: %s", method, provider.name, exc)
                if failures is not None:
                    failures.record(provider.name, exc)
                continue
            for candidate in candidates:
                if _is_same_item(candidate, track):
                    continue
                if method == "find_similar_tracks" and match_track(track, candidate).matched:
                    continue
                if any(match_track(existing, candidate).matched for existing in found):
                    continue
                found.append(candidate)
    return found[:limit]


def appears_on(
    track: Track, registry, auth_manager, priority: list[str],
    *, failures: ProviderFailures | None = None, budget: RequestBudget | None = None,
) -> list[dict]:
    """Le uscite che contengono la stessa registrazione della traccia (una
    traccia di un provider con lo stesso ISRC o MBID), una per titolo e anno,
    dal provider con priorità più alta."""
    isrc = track.get_id(ISRC)
    candidates: list[Track] = [track]
    for provider in _providers_by_priority(registry, priority):
        if skip_provider(budget, provider.name):
            continue
        try:
            session = auth_manager.authenticate(provider)
            candidates.extend(bounded_call(budget, provider.name, provider.find_related_tracks, track, session, 50))
            if isrc is not None:
                by_isrc = bounded_call(budget, provider.name, provider.find_by_isrc, isrc.value, session)
                if by_isrc is not None:
                    candidates.append(by_isrc)
        except (ProviderNotConfigured, ProviderSkipped):
            continue
        except Exception as exc:
            logger.warning("appears_on lookup failed on %s: %s", provider.name, exc)
            if failures is not None:
                failures.record(provider.name, exc)

    candidates.sort(key=lambda t: provider_sort_key(t.provider, priority))
    releases: dict[tuple[str, int | None], dict] = {}
    for candidate in candidates:
        if candidate is not track and not shared_track_identity(track, candidate, ISRC, MBID):
            continue
        if not candidate.album:
            continue
        key = (normalize(candidate.album), candidate.year)
        releases.setdefault(key, {
            "title": candidate.album,
            "year": candidate.year,
            "provider": candidate.provider,
            "provider_id": candidate.album_provider_id,
            "track_provider_id": candidate.provider_id,
        })
    return list(releases.values())
