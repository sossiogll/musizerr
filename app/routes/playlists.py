"""The playlists of the signed-in user (private: owner only; docs/playlists.md)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator

from core.routines import SYNC_PLAYLISTS
from db import playlist_store as store
from routes.content._helpers import get_authenticated_provider
from routes.deps import AuthManagerDep, ConfigDep, CurrentUserDep, RegistryDep, SessionDep, ViewerDep
from services import playlists, routines
from services.provider_calls import provider_call

router = APIRouter()


@contextmanager
def _rules() -> Iterator[None]:
    """A rule of the playlists that said no becomes its HTTP answer."""
    try:
        yield
    except playlists.PlaylistError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body()) from exc


def _push_soon() -> None:
    """A playlist changed: mirror it on the sync providers now (the routine
    does nothing while it is already queued or running)."""
    routines.start_routine(SYNC_PLAYLISTS)


class PlaylistBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)

    @model_validator(mode="after")
    def _named(self):
        if not self.name.strip():
            raise ValueError("name must not be blank")
        return self


class PlaylistPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    # A followed playlist: download what its source gains.
    auto_download: bool | None = None

    @model_validator(mode="after")
    def _something(self):
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        if self.name is None and self.description is None and self.auto_download is None:
            raise ValueError("nothing to change")
        return self


class TrackItem(BaseModel):
    """A track the host knows (``track_id``: the ``library_id`` of a listed
    track) or one a provider names (``provider`` + ``provider_id``)."""
    track_id: str | None = None
    provider: str | None = None
    provider_id: str | None = None

    @model_validator(mode="after")
    def _one_way(self):
        by_provider = bool(self.provider and self.provider_id)
        if bool(self.track_id) == by_provider:
            raise ValueError("give either track_id or provider and provider_id")
        return self


class AddTracksBody(BaseModel):
    items: list[TrackItem] = Field(..., min_length=1, max_length=playlists.MAX_ADD_BATCH)


class OrderBody(BaseModel):
    track_ids: list[str] = Field(..., max_length=10000)


class FollowBody(BaseModel):
    provider: str
    id: str


class DuplicateBody(BaseModel):
    name: str | None = Field(None, max_length=200)


@router.get("/playlists")
def list_playlists(session: SessionDep, user: CurrentUserDep, config: ConfigDep):
    """The user's playlists (own and followed), oldest first, without their
    tracks, and the most tracks one may hold."""
    items = playlists.public_playlists(session, store.list_for_user(session, user.id))
    return {"items": items, "max_tracks": config.playlist_max_tracks}


@router.post("/playlists", status_code=201)
def create_playlist(body: PlaylistBody, session: SessionDep, user: CurrentUserDep):
    """A new, empty playlist of the user's own."""
    playlist = playlists.create(session, user.id, body.name, body.description)
    _push_soon()
    return playlists.public_playlist(session, playlist)


@router.post("/playlists/follow")
def follow_playlist(
    body: FollowBody, response: Response, session: SessionDep, user: CurrentUserDep, config: ConfigDep,
    registry: RegistryDep, auth_manager: AuthManagerDep,
):
    """Follows a playlist of a metadata provider: its tracks are kept (read-only,
    followed as the source changes) and asked for download through the
    approval policy; they do not become favorites. 201 when the follow is new,
    200 when it already was. ``download.status`` says what the download request
    came to. 409 ``playlist_full`` for a playlist longer than the cap."""
    meta, auth_session = get_authenticated_provider(registry, auth_manager, body.provider)
    with provider_call(body.provider):
        source = meta.get_playlist(body.id, auth_session)
    if source is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    with _rules():
        result = playlists.follow(session, user, source, config=config, registry=registry, auth_manager=auth_manager)
    if result.created:
        response.status_code = 201
        _push_soon()
    return {
        "playlist": playlists.public_playlist(session, result.playlist),
        "created": result.created,
        "download": result.download,
    }


@router.get("/playlists/{playlist_id}")
def get_playlist(playlist_id: str, session: SessionDep, user: CurrentUserDep, viewer: ViewerDep, registry: RegistryDep):
    """A playlist with its tracks, in order (annotated like every track list)."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
    return playlists.playlist_detail(session, playlist, viewer, registry)


@router.patch("/playlists/{playlist_id}")
def update_playlist(playlist_id: str, body: PlaylistPatch, session: SessionDep, user: CurrentUserDep):
    """Renames or re-describes an own playlist; on a followed one only
    ``auto_download`` can change (409 ``playlist_read_only`` otherwise)."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        playlist = playlists.update(
            session, playlist, name=body.name, description=body.description, auto_download=body.auto_download,
        )
    _push_soon()
    return playlists.public_playlist(session, playlist)


@router.delete("/playlists/{playlist_id}")
def delete_playlist(
    playlist_id: str, session: SessionDep, user: CurrentUserDep,
    delete_tracks: bool = Query(False, description="Also delete the files downloaded for this playlist."),
):
    """Deletes an own playlist, or unfollows a followed one. By default only the
    playlist goes and the tracks stay in the library; with ``delete_tracks`` the
    files that were downloaded for it, and no other playlist or favorite holds,
    are deleted too (in the background: ``tracks_scheduled``)."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        outcome = playlists.delete(session, playlist, delete_tracks=delete_tracks)
    _push_soon()
    return outcome


@router.post("/playlists/{playlist_id}/tracks")
def add_tracks(
    playlist_id: str, body: AddTracksBody, session: SessionDep, user: CurrentUserDep, config: ConfigDep,
    registry: RegistryDep, auth_manager: AuthManagerDep,
):
    """Appends tracks to an own playlist; the ones already in it are skipped.
    409 ``playlist_full`` when the batch would pass the cap, 409
    ``playlist_read_only`` on a followed playlist."""
    refs = [playlists.TrackRef(item.track_id, item.provider, item.provider_id) for item in body.items]

    def fetch_track(provider: str, provider_id: str):
        meta, auth_session = get_authenticated_provider(registry, auth_manager, provider)
        with provider_call(provider):
            return meta.get_track(provider_id, auth_session)

    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        playlists.ensure_editable(playlist)
        track_ids = playlists.resolve_refs(session, refs, registry, fetch_track)
        outcome = playlists.add_tracks(session, playlist, track_ids, config)
    _push_soon()
    return {**outcome, "playlist": playlists.public_playlist(session, store.get(session, playlist_id))}


@router.put("/playlists/{playlist_id}/tracks/order")
def reorder_tracks(playlist_id: str, body: OrderBody, session: SessionDep, user: CurrentUserDep):
    """Puts the tracks in the given order, which must list exactly the tracks of
    the playlist (422 ``playlist_invalid_order`` otherwise)."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        playlists.reorder(session, playlist, body.track_ids)
    _push_soon()
    return {"track_ids": body.track_ids}


@router.delete("/playlists/{playlist_id}/tracks/{track_id}")
def remove_track(playlist_id: str, track_id: str, session: SessionDep, user: CurrentUserDep):
    """Takes a track out of an own playlist (its file stays in the library).
    Idempotent."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        removed = playlists.remove_track(session, playlist, track_id)
    _push_soon()
    return {"track_id": track_id, "removed": removed}


@router.post("/playlists/{playlist_id}/duplicate", status_code=201)
def duplicate_playlist(
    playlist_id: str, session: SessionDep, user: CurrentUserDep, body: DuplicateBody | None = None,
):
    """An own, editable copy of a playlist (a followed one included), with the
    tracks it has now."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        copy = playlists.duplicate(session, playlist, body.name if body else None)
    _push_soon()
    return playlists.public_playlist(session, copy)


@router.post("/playlists/{playlist_id}/refresh")
def refresh_playlist(
    playlist_id: str, session: SessionDep, user: CurrentUserDep, config: ConfigDep, registry: RegistryDep,
    auth_manager: AuthManagerDep,
):
    """Looks at the source of a followed playlist now (the routine does it
    periodically), reading its tracks whatever the provider's version marker
    says. 422 ``playlist_not_followed`` for an own playlist."""
    with _rules():
        playlist = playlists.owned(session, user.id, playlist_id)
        playlists.ensure_followed(playlist)
        provider = playlist.source_provider
        meta, auth_session = get_authenticated_provider(registry, auth_manager, provider)
        with provider_call(provider):
            result = playlists.refresh(
                session, user, playlist, meta, auth_session, config=config, registry=registry,
                auth_manager=auth_manager, force=True,
            )
    if result.changed:
        _push_soon()
    return {
        "playlist": playlists.public_playlist(session, store.get(session, playlist_id)),
        "changed": result.changed,
        "new_tracks": result.new_tracks,
        "removed_tracks": result.removed_tracks,
        "error_code": result.error_code,
        "download": result.download,
    }
