from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from core.serialization import serialize_item
from routes.deps import AuthManagerDep, ConfigDep, DownloaderDep, RegistryDep, SessionDep, ViewerDep
from services.download_requests import playlist_spec, request_download
from services.provider_calls import provider_call
from services.track_state import annotate_tracks, collection_summary
from ._helpers import annotated_item, get_authenticated_provider
from ._schemas import DownloadBody

router = APIRouter()


@router.get("/playlist/{provider}/{id}")
def get_playlist(provider: str, id: str, registry: RegistryDep, auth_manager: AuthManagerDep, db_session: SessionDep,
                 viewer: ViewerDep):
    p, auth_session = get_authenticated_provider(registry, auth_manager, provider)
    with provider_call(provider):
        playlist = p.get_playlist(id, auth_session)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    serialized = serialize_item(playlist)
    annotate_tracks(serialized["tracks"], db_session, viewer, registry)
    serialized.update(collection_summary(serialized["tracks"], playlist.track_count))
    return serialized


@router.post("/download/playlist")
def download_playlist(
    body: DownloadBody,
    registry: RegistryDep,
    auth_manager: AuthManagerDep,
    session: SessionDep,
    config: ConfigDep,
    user: DownloaderDep,
    viewer: ViewerDep,
    response: Response,
):
    """Chiede il download di una playlist. Per un utente non admin passa
    dalla policy di approvazione (``services/download_requests.py``): se deve
    aspettare un admin risponde 202 ``pending_approval`` e non accoda nulla."""
    p, auth_session = get_authenticated_provider(registry, auth_manager, body.provider)
    with provider_call(body.provider):
        playlist = p.get_playlist(body.id, auth_session)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    outcome = request_download(
        session, user.user, playlist_spec(playlist, only_missing=body.only_missing),
        favorite_for=user.favorite_for(body.add_to_favorites),
        config=config, registry=registry, auth_manager=auth_manager,
    )
    if outcome.pending:
        response.status_code = 202
        return outcome.pending_body()
    batch = outcome.execution.batch
    return {
        "download_ids": batch.download_ids,
        "playlist": annotated_item(playlist, session, viewer, registry),
        "total_tracks": len(batch.download_ids),
        "skipped": batch.skipped,
    }
