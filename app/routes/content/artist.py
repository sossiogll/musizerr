from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException, Query, Response

from core.discography import OWN_RELEASE_TYPES, RELEASES_LISTING_LIMIT, group_releases
from core.provider_failures import ProviderFailures
from core.serialization import serialize_item, serialize_release_group, serialize_track
from routes.deps import AuthManagerDep, ConfigDep, DownloaderDep, RegistryDep, SessionDep, ViewerDep
from routes._pagination import paginated_response
from services.discography import DiscographyError, gather_discography, not_supported
from services.download_requests import artist_spec, request_download
from services.enrichment import (
    complete_universal_ids, cross_enrich_artist, cross_enrich_artist_albums, fill_missing_artist,
)
from services.errors import provider_errors, public_error
from services.provider_calls import provider_call
from services.request_budget import enrichment_budget
from services.track_state import annotate_tracks, known_track_ids
from services.library_coverage import release_coverage, summarize
from services.top_tracks import artist_top_tracks
from ._helpers import get_authenticated_provider
from ._schemas import DownloadBody

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/artist/{provider}/{id}")
def get_artist(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, config: ConfigDep):
    p, session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        artist = p.get_artist(id, session)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    budget = enrichment_budget(config)
    cross_enrich_artist(artist, registry, auth_manager, budget)
    return {
        **serialize_item(artist),
        "discography_downloadable": p.supports_discography(),
        "provider_errors": provider_errors(budget.failures),
    }


@router.get("/artist/{provider}/{id}/albums")
def get_artist_albums(
    provider: str,
    id: str,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    db_session: SessionDep,
    config: ConfigDep,
    viewer: ViewerDep,
    page: int = Query(1, ge=1),
    limit: int | None = Query(None, ge=1, le=200),
):
    """Discografia raggruppata per uscita: le edizioni della stessa uscita
    (anche da altri provider) sono una voce con la lista ``editions``. La
    paginazione conta le uscite, non le edizioni. Il provider è letto fino a
    ``RELEASES_LISTING_LIMIT`` uscite (mai un'impostazione di ricerca): se le
    raggiunge, ``may_be_longer`` dice che l'elenco può continuare."""
    p, session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        artist = p.get_artist(id, session)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    with provider_call(provider):
        albums = p.get_artist_albums(id, session, limit=RELEASES_LISTING_LIMIT)
    may_be_longer = len(albums) >= RELEASES_LISTING_LIMIT
    fill_missing_artist(albums, artist.name, artist.provider_id)
    failures = ProviderFailures()
    cross_enrich_artist_albums(artist.name, provider, albums, registry, auth_manager, failures=failures,
                               budget=enrichment_budget(config, failures))
    logger.info("get_artist_albums %s/%s → %d albums", provider, id, len(albums))
    groups = group_releases(albums, config.metadata_provider_priority)
    coverages = release_coverage(groups, db_session, registry)
    start = (page - 1) * limit if limit else 0
    end = start + limit if limit else len(groups)
    serialized = []
    for group, group_coverage in zip(groups[start:end], coverages[start:end]):
        album_dict = serialize_release_group(group)
        if album_dict.get("tracks"):
            annotate_tracks(album_dict["tracks"], db_session, viewer, registry)
        album_dict["library"] = group_coverage
        serialized.append(album_dict)
    response = paginated_response(serialized, total=len(groups), page=page, limit=limit)
    # Riepilogo dell'artista su tutta la discografia, non solo sulla pagina.
    response["library"] = summarize(coverages)
    response["may_be_longer"] = may_be_longer
    response["provider_errors"] = provider_errors(failures)
    return response


@router.get("/artist/{provider}/{id}/top-tracks")
def get_artist_top_tracks(
    provider: str,
    id: str,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    db_session: SessionDep,
    config: ConfigDep,
    viewer: ViewerDep,
    limit: int = Query(10, ge=1, le=50),
):
    """Tracce più popolari dell'artista (vedi services/top_tracks.py), con lo
    stato di download. Un provider che fallisce non rompe la pagina: se
    falliscono tutti, lista vuota con ``error_code``."""
    p, session = get_authenticated_provider(registry, auth_manager, provider)
    try:
        artist = p.get_artist(id, session)
    except Exception as exc:
        logger.warning("get_artist %s/%s failed: %s", provider, id, exc)
        failures = ProviderFailures()
        failures.record(provider, exc)
        return {"items": [], **public_error(exc, "metadata_provider", show_detail=False),
                "provider_errors": provider_errors(failures)}
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")

    result = artist_top_tracks(artist, p, registry, auth_manager, config.metadata_provider_priority, limit)
    items = [serialize_track(t) for t in result.tracks]
    # Una riga di un provider che negli elenchi omette l'ISRC (es. il ripiego
    # su un secondo provider) non si riconoscerebbe in libreria: la si
    # completa dal dettaglio, solo se il database non la conosce già.
    unknown = [t for t, known in zip(result.tracks, known_track_ids(items, db_session, registry)) if known is None]
    if unknown:
        complete_universal_ids(unknown, registry, auth_manager, enrichment_budget(config, result.failures))
        items = [serialize_track(t) for t in result.tracks]
    annotate_tracks(items, db_session, viewer, registry)
    body = {"items": items, "error_code": None, "error_message": None,
            "provider_errors": provider_errors(result.failures)}
    if result.failure is not None:
        body.update(public_error(result.failure, "metadata_provider", show_detail=False))
    return body


@router.post("/download/artist")
def download_artist(
    body: DownloadBody,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    session: SessionDep,
    config: ConfigDep,
    user: DownloaderDep,
    response: Response,
):
    """
    Chiede il download di tutta la discografia di un artista: le uscite
    dell'artista sul provider richiesto (``release_types``, di default album,
    singoli ed EP), con le tracce di ciascuna, ogni brano una volta sola
    (``services/discography.py``). Solo per un provider che dichiara
    ``supports_discography`` (altrimenti 422 ``discography_not_supported``).
    Un'uscita che non si legge non ferma le altre: la risposta la conta
    (``releases_failed``) e dice perché (``provider_errors``).
    Per un utente non admin passa dalla policy di approvazione
    (``services/download_requests.py``): se deve aspettare un admin risponde
    202 ``pending_approval`` e non accoda nulla.
    """
    p, auth_session = get_authenticated_provider(registry, auth_manager, body.provider)
    if not p.supports_discography():
        raise HTTPException(status_code=422, detail=not_supported(body.provider).detail())
    with provider_call(body.provider):
        artist = p.get_artist(body.id, auth_session)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    try:
        discography = gather_discography(
            artist, p, auth_session, priority=config.metadata_provider_priority,
            release_types=body.release_types or OWN_RELEASE_TYPES,
        )
    except DiscographyError as exc:
        raise HTTPException(status_code=422, detail=exc.detail()) from exc
    gathered = {
        "releases": discography.releases,
        "releases_failed": discography.releases_failed,
        "provider_errors": provider_errors(discography.failures),
    }

    outcome = request_download(
        session, user.user, artist_spec(artist, discography.tracks),
        favorite_for=user.favorite_for(body.add_to_favorites),
        config=config, registry=registry, auth_manager=auth_manager,
    )
    if outcome.pending:
        response.status_code = 202
        return {**outcome.pending_body(), **gathered}
    batch = outcome.execution.batch
    return {
        "download_ids": batch.download_ids,
        "artist": serialize_item(artist),
        "total_tracks": len(batch.download_ids),
        "skipped": batch.skipped,
        **gathered,
    }
