from __future__ import annotations
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from core.provider_failures import ProviderFailures
from core.serialization import serialize_item
from services.errors import provider_errors
from services.enrichment import enrich_tracks_with_stream_url
from services.track_state import annotate_tracks
from services.request_budget import enrichment_budget
from .deps import AuthManagerDep, ConfigDep, RegistryDep, SearchAggregatorDep, SessionDep, ViewerDep
from ._pagination import paginated_response

router = APIRouter()

SearchType = Literal["track", "album", "artist", "playlist"]


@router.get("/search")
def search(
    aggregator: SearchAggregatorDep,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    session: SessionDep,
    config: ConfigDep,
    viewer: ViewerDep,
    q: str = Query(..., min_length=1),
    type: SearchType = Query("track"),
    providers: list[str] | None = Query(None),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    failures = ProviderFailures()
    match type:
        case "track":
            items = aggregator.search_tracks(q, provider_filter=providers, failures=failures)
            # The stream URLs are extra: bounded by their own budget, after the search's.
            enrich_tracks_with_stream_url(items, registry, auth_manager, enrichment_budget(config, failures))
        case "album":
            items = aggregator.search_albums(q, provider_filter=providers, failures=failures)
            tracks = [t for album in items for t in album.tracks]
            enrich_tracks_with_stream_url(tracks, registry, auth_manager, enrichment_budget(config, failures))
        case "artist":
            items = aggregator.search_artists(q, provider_filter=providers, failures=failures)
        case "playlist":
            items = aggregator.search_playlists(q, provider_filter=providers, failures=failures)

    serialized = [serialize_item(i) for i in items]
    if type == "track":
        annotate_tracks(serialized, session, viewer, registry)
    elif type == "album":
        for album_dict in serialized:
            annotate_tracks(album_dict["tracks"], session, viewer, registry)
    return {"type": type, "query": q, **paginated_response(serialized), "provider_errors": provider_errors(failures)}


def _budget(config, failures: ProviderFailures) -> RequestBudget:
    return RequestBudget(config.enrichment_budget_seconds, failures)