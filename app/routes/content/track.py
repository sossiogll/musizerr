from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException, Query, Response

from musizerr_sdk.auth import AuthSession
from musizerr_sdk.identifiers import Identifier
from core.provider_failures import ProviderFailures
from core.serialization import serialize_item, serialize_lyrics, serialize_track
from routes.deps import AuthManagerDep, ConfigDep, DownloaderDep, RegistryDep, SessionDep, ViewerDep
from services.download_requests import request_download, track_spec
from services.enrichment import cross_enrich_track, enrich_tracks_with_stream_url
from services.errors import provider_errors
from services.provider_calls import ProviderCallError, provider_call
from services.related import appears_on, related_tracks
from services.request_budget import enrichment_budget
from services.track_state import annotate_tracks
from ._helpers import annotated_item, get_authenticated_provider
from ._schemas import DownloadBody

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/track/{provider}/{id}")
def get_track(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, db_session: SessionDep,
              config: ConfigDep, viewer: ViewerDep):
    p, auth_session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        track = p.get_track(id, auth_session)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    budget = enrichment_budget(config)
    cross_enrich_track(track, registry, auth_manager, budget)
    enrich_tracks_with_stream_url([track], registry, auth_manager, budget)
    serialized = serialize_item(track)
    annotate_tracks([serialized], db_session, viewer, registry)
    serialized["provider_errors"] = provider_errors(budget.failures)
    return serialized


@router.get("/track/{provider}/{id}/lyrics")
def get_track_lyrics(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep):
    meta, session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        track = meta.get_track(id, session)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    # Il primo provider di testi che risponde vince; uno che fallisce non
    # nasconde gli altri. Se nessuno trova il testo e almeno uno ha fallito,
    # "non trovato" non sarebbe vero: si risponde col suo errore.
    failure: ProviderCallError | None = None
    for lp in registry.all_lyrics():
        try:
            with provider_call(lp.name, subject="lyrics_provider"):
                lp_session = auth_manager.authenticate(lp) if lp.auth.flow != lp.auth.flow.NONE else AuthSession.anonymous()
                lyrics = lp.get_lyrics(track, lp_session)
        except ProviderCallError as exc:
            failure = failure or exc
            continue
        if lyrics:
            return serialize_lyrics(lyrics)

    if failure is not None and failure.kind != "not_configured":
        raise failure
    raise HTTPException(status_code=404, detail="Lyrics not found")


@router.post("/download/track")
def download_track(
    body: DownloadBody,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    session: SessionDep,
    config: ConfigDep,
    user: DownloaderDep,
    viewer: ViewerDep,
    response: Response,
):
    """Chiede il download di una traccia. Una traccia già in libreria non si
    riscarica (``status: already_in_library``, nessun download), salvo
    ``force`` (solo admin); una con una richiesta aperta vi si aggancia
    (``attached``). Per un utente non admin la richiesta passa dalla policy di
    approvazione (``services/download_requests.py``): se deve aspettare un
    admin risponde 202 ``pending_approval`` e non accoda nulla."""
    user.check_force(body.force)
    p, auth_session = get_authenticated_provider(registry, auth_manager, body.provider)
    with provider_call(body.provider):
        track = p.get_track(body.id, auth_session)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    # Integra gli external_ids già raccolti dal frontend (cross-enrich)
    if body.external_ids:
        for eid in body.external_ids:
            try:
                tid = Identifier.from_dict({"type": eid.type, "value": eid.value})
                track.add_id(tid)
            except (ValueError, KeyError):
                pass  # tipo sconosciuto — ignora

    outcome = request_download(
        session, user.user, track_spec(track, force=body.force), favorite_for=user.favorite_for(body.add_to_favorites),
        config=config, registry=registry, auth_manager=auth_manager,
    )
    if outcome.pending:
        response.status_code = 202
        return outcome.pending_body()
    return {**outcome.execution.batch.results[0].as_dict(), "track": annotated_item(track, session, viewer, registry)}


@router.get("/track/{provider}/{id}/related")
def get_related_tracks(
    provider: str,
    id: str,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    db_session: SessionDep,
    config: ConfigDep,
    viewer: ViewerDep,
    limit: int = Query(20, ge=1, le=50),
):
    """Varianti correlate della traccia da tutti i provider di metadati, in
    ordine di priorità, deduplicate e con lo stato di download."""
    failures = ProviderFailures()
    track, budget = _enriched_track(provider, id, registry, auth_manager, config, failures)
    items = [serialize_track(t) for t in related_tracks(
        track, registry, auth_manager, config.metadata_provider_priority, limit, failures=failures, budget=budget,
    )]
    annotate_tracks(items, db_session, viewer, registry)
    return {"items": items, "provider_errors": provider_errors(failures)}


@router.get("/track/{provider}/{id}/appears-on")
def get_appears_on(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, config: ConfigDep):
    """Le uscite che contengono la stessa registrazione (stesso ISRC o MBID)."""
    failures = ProviderFailures()
    track, budget = _enriched_track(provider, id, registry, auth_manager, config, failures)
    items = appears_on(track, registry, auth_manager, config.metadata_provider_priority, failures=failures, budget=budget)
    return {"items": items, "provider_errors": provider_errors(failures)}


def _enriched_track(provider: str, id: str, registry, auth_manager, config, failures: ProviderFailures):
    """La traccia con gli identificatori degli altri provider, per trovarla
    anche lì, e il budget della richiesta da cui è partito l'arricchimento:
    la ricerca che segue lo usa per il tempo che resta, e salta chi ha già
    fallito (chi manca va in ``failures``)."""
    p, auth_session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        track = p.get_track(id, auth_session)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    budget = enrichment_budget(config, failures)
    cross_enrich_track(track, registry, auth_manager, budget)
    return track, budget
