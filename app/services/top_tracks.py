"""
Tracce più popolari di un artista, per la sezione "Popular tracks": prima dal
provider richiesto, poi, finché ne mancano, dagli altri provider di
metadati in ordine di priorità. L'artista si ritrova sugli altri provider
con l'arricchimento incrociato (i loro id finiscono fra gli identificatori
dell'artista); i doppioni si tolgono col matching di core.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.auth_manager import ProviderNotConfigured
from musizeer_sdk.identifiers import PROVIDER_ID
from core.matching import match_track
from musizeer_sdk.models import Artist, Track
from core.provider_failures import ProviderFailures
from core.utils import provider_sort_key
from services.enrichment import cross_enrich_artist

logger = logging.getLogger(__name__)


@dataclass
class TopTracks:
    tracks: list[Track] = field(default_factory=list)
    # Testo grezzo dell'ultimo errore, solo se TUTTI i provider interrogati
    # sono falliti (altrimenti una lista vuota vuol dire "nessuna traccia").
    failure: BaseException | None = None
    # Ogni provider interrogato che ha fallito, anche quando altri hanno
    # risposto: la risposta dice che la lista può essere incompleta.
    failures: ProviderFailures = field(default_factory=ProviderFailures)


def artist_top_tracks(artist: Artist, requested, registry, auth_manager, priority: list[str], limit: int) -> TopTracks:
    others = sorted(
        (p for p in registry.all_metadata() if p is not requested),
        key=lambda p: provider_sort_key(p.name, priority),
    )
    found: list[Track] = []
    attempted: list[str] = []
    failures = ProviderFailures()
    enriched = False
    last_failure: BaseException | None = None

    for provider in [requested, *others]:
        if len(found) >= limit:
            break
        if provider is requested:
            artist_id = artist.provider_id
        else:
            if not enriched:
                cross_enrich_artist(artist, registry, auth_manager)
                enriched = True
            identifier = artist.get_id(PROVIDER_ID, provider.name)
            if identifier is None:
                continue
            artist_id = identifier.value
        try:
            candidates = provider.get_artist_top_tracks(artist_id, auth_manager.authenticate(provider), limit)
        except ProviderNotConfigured:
            continue
        except Exception as exc:
            attempted.append(provider.name)
            logger.warning("get_artist_top_tracks failed on %s: %s", provider.name, exc)
            failures.record(provider.name, exc)
            last_failure = exc
            continue
        attempted.append(provider.name)
        for candidate in candidates:
            if not any(match_track(existing, candidate).matched for existing in found):
                found.append(candidate)

    all_failed = bool(attempted) and len(failures) == len(attempted)
    return TopTracks(tracks=found[:limit], failure=last_failure if all_failed else None, failures=failures)
