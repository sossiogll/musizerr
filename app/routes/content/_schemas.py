"""Shared request schemas for content routes."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from musizerr_sdk.models import ALBUM_TYPES


class ExternalIdBody(BaseModel):
    type: str
    value: str


class DownloadBody(BaseModel):
    provider: str
    id: str
    external_ids: list[ExternalIdBody] | None = None
    # Album and playlist: skip the tracks already in the library.
    only_missing: bool = False
    # Admin only: add the tracks to your favorites when they arrive. A user's
    # download always does (services.capabilities.favorite_on_arrival).
    add_to_favorites: bool | None = None
    # Admin only: download again a track already in the library (a better
    # copy, a broken file). Without it a track in the library is never
    # downloaded twice.
    force: bool = False
    # Artist only: which kinds of release make the discography. Default: the
    # artist's own albums, singles and EPs (core.discography.OWN_RELEASE_TYPES).
    release_types: list[str] | None = Field(None, min_length=1)

    @field_validator("release_types")
    @classmethod
    def _known_release_types(cls, value: list[str] | None) -> list[str] | None:
        unknown = sorted(set(value or []) - set(ALBUM_TYPES))
        if unknown:
            raise ValueError(f"unknown release types: {', '.join(unknown)} (known: {', '.join(ALBUM_TYPES)})")
        return value
