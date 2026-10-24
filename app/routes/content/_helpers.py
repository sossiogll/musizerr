"""Shared helpers for content routes."""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session

from musizeer_sdk.auth import AuthSession
from musizeer_sdk.interfaces import MetadataProvider
from musizeer_sdk.models import Album, Playlist, Track
from core.serialization import serialize_item
from services.capabilities import Viewer
from services.provider_calls import provider_call
from services.track_state import annotate_tracks


def get_authenticated_provider(registry, auth_manager, provider: str) -> tuple[MetadataProvider, AuthSession]:
    meta = registry.get_metadata_provider(provider)
    if not meta:
        raise HTTPException(404, f"Metadata provider '{provider}' not found")
    with provider_call(provider):
        session = auth_manager.authenticate(meta)
    return meta, session


def annotated_item(item: Track | Album | Playlist, session: Session, viewer: Viewer, registry) -> dict:
    """The serialized item with the state of its tracks (``annotate_tracks``:
    library, file state, favorite, pending favorite, playback): the track
    itself, or every track of an album or playlist. A download response
    answers with it, so the caller sees the state its request produced
    (e.g. the favorite on arrival) without reading the track again."""
    data = serialize_item(item)
    annotate_tracks([data] if isinstance(item, Track) else data.get("tracks") or [], session, viewer, registry)
    return data
