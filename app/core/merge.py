from __future__ import annotations

from musizeer_sdk.models import Album, AlbumRef, Artist, ArtistRef, Track


def _merge_field(existing: Track | Album | Artist, candidate: Track | Album | Artist, field: str) -> None:
    """Fill a missing field on *existing* from *candidate*."""
    if not getattr(existing, field) and getattr(candidate, field):
        setattr(existing, field, getattr(candidate, field))


def enrich(existing: Track | Album | Artist, candidate: Track | Album | Artist) -> None:
    if isinstance(existing, Track) and isinstance(candidate, Track):
        enrich_track(existing, candidate)
        return
    if isinstance(existing, Album) and isinstance(candidate, Album):
        enrich_album(existing, candidate)
        return
    if isinstance(existing, Artist) and isinstance(candidate, Artist):
        enrich_artist(existing, candidate)
        return
    raise TypeError(
        f"Unsupported enrich types: {type(existing)!r} <- {type(candidate)!r}"
    )


def enrich_track(existing: Track, candidate: Track) -> None:
    _merge_external_ids(existing, candidate)
    _merge_provider(existing, candidate)
    _merge_track_albums(existing, candidate)
    _merge_track_artists(existing, candidate)

    _merge_field(existing, candidate, "cover_url")
    _merge_field(existing, candidate, "preview_url")
    _merge_field(existing, candidate, "stream_url")
    _merge_provider_scoped(existing, candidate, "artist_provider_id", "album_provider_id")
    if existing.year is None and candidate.year is not None:
        existing.year = candidate.year
    if existing.track_number is None and candidate.track_number is not None:
        existing.track_number = candidate.track_number
    if existing.disc_number is None and candidate.disc_number is not None:
        existing.disc_number = candidate.disc_number


def enrich_album(existing: Album, candidate: Album) -> None:
    _merge_external_ids(existing, candidate)
    _merge_provider(existing, candidate)

    _merge_field(existing, candidate, "cover_url")
    _merge_provider_scoped(existing, candidate, "artist_provider_id")
    if existing.year is None and candidate.year is not None:
        existing.year = candidate.year
    if existing.track_count is None and candidate.track_count is not None:
        existing.track_count = candidate.track_count
    if existing.album_type is None and candidate.album_type is not None:
        existing.album_type = candidate.album_type


def _merge_provider_scoped(existing: Track | Album, candidate: Track | Album, *fields: str) -> None:
    """Ids that only mean something on their own provider (the artist or the
    album of an item) are filled only from an item of the same provider:
    another provider's artist id would link the item to a page that does
    not exist."""
    if candidate.provider != existing.provider:
        return
    for field in fields:
        _merge_field(existing, candidate, field)


def enrich_artist(existing: Artist, candidate: Artist) -> None:
    _merge_external_ids(existing, candidate)
    _merge_provider(existing, candidate)
    _merge_field(existing, candidate, "cover_url")


def _merge_external_ids(existing: Track | Album | Artist, candidate: Track | Album | Artist) -> None:
    for identifier in candidate.external_ids:
        existing.add_id(identifier)


def _merge_provider(existing: Track | Album | Artist, candidate: Track | Album | Artist) -> None:
    if candidate.provider and candidate.provider not in existing.providers:
        existing.providers.append(candidate.provider)


def _merge_track_albums(existing: Track, candidate: Track) -> None:
    if not candidate.album:
        return
    if any(album.name == candidate.album for album in existing.albums):
        return
    existing.albums.append(
        AlbumRef(
            name=candidate.album,
            provider=candidate.provider,
            provider_id=candidate.album_provider_id,
        )
    )


def _merge_track_artists(existing: Track, candidate: Track) -> None:
    for artist in candidate.artists:
        name_lower = artist.name.lower()
        # Skip if name matches or is contained in an existing compound name
        already = any(
            name_lower == a.name.lower()
            or name_lower in a.name.lower()
            or a.name.lower() in name_lower
            for a in existing.artists
        )
        if not already:
            existing.artists.append(
                ArtistRef(
                    name=artist.name,
                    provider=artist.provider,
                    provider_id=artist.provider_id,
                )
            )
