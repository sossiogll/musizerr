from __future__ import annotations

import uuid

from .discography import ReleaseGroup
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.models import Album, AlbumRef, Artist, ArtistRef, Lyrics, Playlist, Track

# Tipo esplicito di ogni elemento serializzato: il client non deve dedurlo
# dai campi presenti (una playlist ha "name" come un artista).
KIND_TRACK = "track"
KIND_ALBUM = "album"
KIND_ARTIST = "artist"
KIND_PLAYLIST = "playlist"


def serialize_track(track: Track) -> dict:
    return {
        "kind": KIND_TRACK,
        "internal_id": track.internal_id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration_ms": track.duration_ms,
        "cover_url": track.cover_url,
        "preview_url": track.preview_url,
        "stream_url": track.stream_url,
        "provider": track.provider,
        "provider_id": track.provider_id,
        "artist_provider_id": track.artist_provider_id,
        "album_provider_id": track.album_provider_id,
        "year": track.year,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "providers": list(track.providers),
        "external_ids": [identifier.to_dict() for identifier in track.external_ids],
        "albums": [album.to_dict() for album in track.albums],
        "artists": [artist.to_dict() for artist in track.artists],
    }


def deserialize_track(data: dict) -> Track:
    track = Track(
        internal_id=data.get("internal_id", str(uuid.uuid4())),
        title=data.get("title", ""),
        artist=data.get("artist", ""),
        album=data.get("album", ""),
        duration_ms=data.get("duration_ms", 0),
        cover_url=data.get("cover_url"),
        preview_url=data.get("preview_url"),
        provider=data.get("provider", ""),
        provider_id=data.get("provider_id", ""),
        artist_provider_id=data.get("artist_provider_id", ""),
        album_provider_id=data.get("album_provider_id", ""),
        year=data.get("year"),
        track_number=data.get("track_number"),
        disc_number=data.get("disc_number"),
        providers=data.get("providers", []),
    )
    for id_data in data.get("external_ids", []):
        track.external_ids.append(Identifier.from_dict(id_data))
    for a in data.get("albums", []):
        track.albums.append(AlbumRef(**a))
    for a in data.get("artists", []):
        track.artists.append(ArtistRef(**a))
    return track


def serialize_album(album: Album) -> dict:
    return {
        "kind": KIND_ALBUM,
        "internal_id": album.internal_id,
        "title": album.title,
        "artist": album.artist,
        "year": album.year,
        "track_count": album.track_count,
        "album_type": album.album_type,
        "cover_url": album.cover_url,
        "provider": album.provider,
        "provider_id": album.provider_id,
        "artist_provider_id": album.artist_provider_id,
        "providers": list(album.providers),
        "external_ids": [identifier.to_dict() for identifier in album.external_ids],
        "artists": [artist.to_dict() for artist in album.artists],
        "tracks": [serialize_track(track) for track in album.tracks],
    }


def deserialize_album(data: dict) -> Album:
    """The album ``serialize_album`` wrote (its tracks included)."""
    album = Album(
        internal_id=data.get("internal_id", str(uuid.uuid4())),
        title=data.get("title", ""),
        artist=data.get("artist", ""),
        year=data.get("year"),
        track_count=data.get("track_count"),
        album_type=data.get("album_type"),
        cover_url=data.get("cover_url"),
        provider=data.get("provider", ""),
        provider_id=data.get("provider_id", ""),
        artist_provider_id=data.get("artist_provider_id", ""),
        providers=data.get("providers", []),
        tracks=[deserialize_track(track) for track in data.get("tracks", [])],
    )
    album.external_ids.extend(Identifier.from_dict(id_data) for id_data in data.get("external_ids", []))
    album.artists.extend(ArtistRef(**artist) for artist in data.get("artists", []))
    return album


def serialize_artist(artist: Artist) -> dict:
    return {
        "kind": KIND_ARTIST,
        "internal_id": artist.internal_id,
        "name": artist.name,
        "provider": artist.provider,
        "provider_id": artist.provider_id,
        "cover_url": artist.cover_url,
        "providers": list(artist.providers),
        "external_ids": [identifier.to_dict() for identifier in artist.external_ids],
    }


def serialize_playlist(playlist: Playlist) -> dict:
    return {
        "kind": KIND_PLAYLIST,
        "internal_id": playlist.internal_id,
        "name": playlist.name,
        "provider": playlist.provider,
        "provider_id": playlist.provider_id,
        "description": playlist.description,
        "cover_url": playlist.cover_url,
        "track_count": playlist.track_count,
        "tracks_unavailable": (
            {"code": playlist.tracks_unavailable.code, "message": playlist.tracks_unavailable.message}
            if playlist.tracks_unavailable else None
        ),
        "tracks": [serialize_track(track) for track in playlist.tracks],
    }


def serialize_lyrics(lyrics: Lyrics) -> dict:
    return {
        "plain": lyrics.plain,
        "synced": [{"time_ms": line.time_ms, "text": line.text} for line in lyrics.synced],
        "source": lyrics.source,
    }


def serialize_item(item: Track | Album | Artist | Playlist) -> dict:
    if isinstance(item, Track):
        return serialize_track(item)
    if isinstance(item, Album):
        return serialize_album(item)
    if isinstance(item, Artist):
        return serialize_artist(item)
    if isinstance(item, Playlist):
        return serialize_playlist(item)
    raise TypeError(f"Unsupported item type for serialization: {type(item)!r}")


def serialize_release_group(group: ReleaseGroup) -> dict:
    """L'album rappresentante, con tipo, anno (dell'edizione più vecchia) e
    identificatori uniti di tutte le edizioni, più la lista ``editions`` (una
    voce per provider ed edizione, ognuna col proprio anno)."""
    data = serialize_album(group.representative)
    data["album_type"] = group.album_type
    data["year"] = group.year
    data["external_ids"] = [identifier.to_dict() for identifier in group.external_ids]
    data["editions"] = [
        {
            "provider": album.provider,
            "provider_id": album.provider_id,
            "title": album.title,
            "year": album.year,
            "album_type": album.album_type,
            "track_count": album.track_count,
            "cover_url": album.cover_url,
            "external_ids": [identifier.to_dict() for identifier in album.external_ids],
        }
        for album in group.editions
    ]
    return data
