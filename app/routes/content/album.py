from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from core.discography import group_releases

from core.serialization import serialize_item, serialize_release_group
from routes.deps import AuthManagerDep, ConfigDep, DownloaderDep, RegistryDep, SessionDep, ViewerDep
from services.download_requests import album_spec, request_download
from services.provider_calls import provider_call
from services.enrichment import cross_enrich_album, enrich_tracks_with_stream_url, fill_missing_artist
from services.errors import provider_errors
from services.request_budget import enrichment_budget
from services.track_state import annotate_tracks, collection_summary
from ._helpers import annotated_item, get_authenticated_provider
from ._schemas import DownloadBody

router = APIRouter()


@router.get("/album/{provider}/{id}")
def get_album(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, db_session: SessionDep,
              config: ConfigDep, viewer: ViewerDep):
    p, auth_session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        album = p.get_album(id, auth_session)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    budget = enrichment_budget(config)
    enrichment = cross_enrich_album(album, registry, auth_manager, budget)
    enrich_tracks_with_stream_url(album.tracks, registry, auth_manager, budget)
    serialized = serialize_item(album)
    annotate_tracks(serialized["tracks"], db_session, viewer, registry)
    serialized.update(collection_summary(serialized["tracks"], album.track_count))
    serialized["enrichment"] = enrichment.as_dict()
    serialized["provider_errors"] = provider_errors(budget.failures)
    return serialized


# Releases of the artist read to find the editions of one album.
EDITIONS_SCAN_LIMIT = 50


@router.get("/album/{provider}/{id}/editions")
def get_album_editions(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, config: ConfigDep):
    """Le edizioni della stessa uscita (standard, deluxe, remaster...) sul
    provider dell'album, raggruppate come nella discografia dell'artista
    (core.discography.group_releases). Nessun arricchimento cross-provider:
    due chiamate al provider, abbastanza leggere per la pagina dell'album."""
    p, auth_session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        album = p.get_album(id, auth_session)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    if not album.artist_provider_id:
        return {"items": []}
    with provider_call(provider):
        releases = p.get_artist_albums(album.artist_provider_id, auth_session, limit=EDITIONS_SCAN_LIMIT)
    if not any(r.provider_id == id for r in releases):
        releases = [album, *releases]
    fill_missing_artist(releases, album.artist, album.artist_provider_id)
    group = next(
        (g for g in group_releases(releases, config.metadata_provider_priority) if any(e.provider_id == id for e in g.editions)),
        None,
    )
    editions = serialize_release_group(group)["editions"] if group else []
    editions.sort(key=lambda e: (e["year"] or 9999, e["title"]))
    return {"items": editions}


@router.post("/download/album")
def download_album(
    body: DownloadBody,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    session: SessionDep,
    config: ConfigDep,
    user: DownloaderDep,
    viewer: ViewerDep,
    response: Response,
):
    """Chiede il download di un album: l'album intero a un sistema esterno se
    il provider di download in cima all'ordine lo prende, altrimenti una
    traccia alla volta (``services.downloads.download_album``). Per un utente
    non admin passa prima dalla policy di approvazione
    (``services/download_requests.py``), consegna a un sistema esterno
    compresa: se deve aspettare un admin risponde 202 ``pending_approval`` e
    non accoda né delega nulla."""
    p, auth_session = get_authenticated_provider(registry, auth_manager, body.provider)
    with provider_call(body.provider):
        album = p.get_album(body.id, auth_session)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    outcome = request_download(
        session, user.user, album_spec(album, only_missing=body.only_missing),
        favorite_for=user.favorite_for(body.add_to_favorites),
        config=config, registry=registry, auth_manager=auth_manager,
    )
    if outcome.pending:
        response.status_code = 202
        return outcome.pending_body()
    execution = outcome.execution
    if execution.delegated is not None:
        return {
            "download_ids": [],
            "album": annotated_item(album, session, viewer, registry),
            "total_tracks": len(album.tracks),
            "dispatched_to": execution.delegated.provider,
            "details": execution.delegated.details,
        }
    return {
        "download_ids": execution.batch.download_ids,
        "album": annotated_item(album, session, viewer, registry),
        "total_tracks": len(execution.batch.download_ids),
        "skipped": execution.batch.skipped,
    }
