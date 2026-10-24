from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .identifiers import Identifier


class HasIdentifiers:
    """Mixin per entità con external_ids — deduplicato per Track, Album, Artist."""

    external_ids: list[Identifier]

    def get_id(self, id_type: str, provider: str | None = None) -> Identifier | None:
        """Cerca un identifier per tipo (e opzionalmente provider).

        Esempi::

            track.get_id("isrc")                    # primo ISRC
            track.get_id("provider_id", "catalog")  # the id on that catalog
        """
        return next(
            (
                identifier
                for identifier in self.external_ids
                if identifier.type == id_type
                and (provider is None or identifier.provider == provider)
            ),
            None,
        )

    def add_id(self, identifier: Identifier) -> None:
        already = any(
            e.type == identifier.type
            and e.value == identifier.value
            and e.provider == identifier.provider
            for e in self.external_ids
        )
        if not already:
            self.external_ids.append(identifier)


@dataclass
class AlbumRef:
    name: str
    provider: str
    provider_id: str

    def to_dict(self) -> dict:
        return {"name": self.name, "provider": self.provider, "provider_id": self.provider_id}


@dataclass
class ArtistRef:
    name: str
    provider: str
    provider_id: str

    def to_dict(self) -> dict:
        return {"name": self.name, "provider": self.provider, "provider_id": self.provider_id}


@dataclass
class Track(HasIdentifiers):
    title: str
    artist: str
    album: str
    duration_ms: int
    cover_url: str | None = None
    preview_url: str | None = None
    stream_url: str | None = None
    provider: str = ""
    provider_id: str = ""
    artist_provider_id: str = ""
    album_provider_id: str = ""
    internal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    external_ids: list[Identifier] = field(default_factory=list)
    year: int | None = None
    albums: list[AlbumRef] = field(default_factory=list)
    artists: list[ArtistRef] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    saved_at: datetime | None = None
    # Posizione nell'album, se il provider la conosce.
    track_number: int | None = None
    disc_number: int | None = None

    def to_dict(self) -> dict:
        return {
            "internal_id": self.internal_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "year": self.year,
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "external_ids": [i.to_dict() for i in self.external_ids],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Track:
        track = cls(
            internal_id=data["internal_id"],
            title=data["title"],
            artist=data["artist"],
            album=data["album"],
            duration_ms=data["duration_ms"],
            year=data.get("year"),
            track_number=data.get("track_number"),
            disc_number=data.get("disc_number"),
        )
        for id_data in data.get("external_ids", []):
            track.external_ids.append(Identifier.from_dict(id_data))
        return track


# Tipi di uscita generici (Album.album_type). "appears_on": l'artista
# compare in un'uscita di altri.
ALBUM_TYPES = ("album", "single", "ep", "compilation", "live", "appears_on")


@dataclass
class Album(HasIdentifiers):
    title: str
    artist: str
    year: int | None = None
    track_count: int | None = None
    cover_url: str | None = None
    provider: str = ""
    provider_id: str = ""
    artist_provider_id: str = ""
    internal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    external_ids: list[Identifier] = field(default_factory=list)
    artists: list[ArtistRef] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    # Tipo di uscita, se il provider lo conosce: uno di ALBUM_TYPES.
    album_type: str | None = None


@dataclass
class Artist(HasIdentifiers):
    name: str
    provider: str = ""
    provider_id: str = ""
    cover_url: str | None = None
    internal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    external_ids: list[Identifier] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)


@dataclass
class LrcLine:
    time_ms: int
    text: str


@dataclass
class Lyrics:
    plain: str | None = None
    synced: list[LrcLine] = field(default_factory=list)
    source: str = ""


@dataclass
class LibraryItem:
    """
    Un file audio così come il SyncProvider lo vede nella libreria già
    indicizzata (tag + info tecniche), SENZA aver scaricato i byte.
    Provider-agnostico: usato dal dedup di libreria (vedi
    ``core.duplicates`` per il clustering, ``core.audio_quality`` per la
    qualità) per ottenere candidati economicamente, senza leggere ogni file.

    ATTENZIONE: ``path`` è spesso un path VIRTUALE, ricostruito dal sync
    provider a partire dai tag (es. Navidrome via Subsonic non riporta il
    path reale su disco per default) — NON è garantito combaciare con lo
    spazio dei path che lo ``StorageProvider`` usa per
    ``store``/``exists``/``delete`` (verificato dal vivo: gli stessi file
    scaricati da musizerr risultano non trovabili sullo storage seguendo
    questo path). Va trattato solo come ETICHETTA per la UI/i log — per
    leggere i byte reali usare ``SyncProvider.fetch_partial(sync_id, ...)``,
    mai questo path; per l'origine/cancellabilità usare ``sync_id`` per
    risalire a un ``Download`` musizerr (vedi
    ``db.duplicate_store.resolve_download_for_item``), mai un confronto path.

    ``bitrate``/``sample_rate`` sono 0 quando il sync provider non li
    riporta (mai un valore inventato) — vedi ``core.audio_quality.
    parse_bitrate``/``parse_sample_rate`` per interpretarli da valori grezzi.
    """

    sync_id: str
    path: str
    title: str
    artist: str
    album: str = ""
    year: int | None = None
    duration_ms: int = 0
    size: int = 0
    suffix: str = ""     # estensione/codec riportato dal sync provider, es. "flac", "mp3"
    bitrate: int = 0
    sample_rate: int = 0

    def to_track(self) -> Track:
        """Track di dominio minimale per riusare il matcher universale
        (``core.matching``) sul clustering — provider/provider_id vuoti
        perché un LibraryItem non ha un'origine di metadati, solo un sync_id."""
        return Track(
            title=self.title,
            artist=self.artist,
            album=self.album,
            duration_ms=self.duration_ms,
            year=self.year,
            provider_id=self.sync_id,
        )


@dataclass(frozen=True)
class TracksUnavailable:
    """Perché le tracce di una raccolta non si possono leggere. ``code`` è
    stabile e generico: ``access_restricted`` (il servizio non le rende a
    questa app o a questo account), ``error`` (la lettura è fallita)."""

    code: str
    message: str


TRACKS_ACCESS_RESTRICTED = "access_restricted"
TRACKS_READ_ERROR = "error"


@dataclass
class Playlist:
    name: str
    provider: str
    provider_id: str
    internal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    cover_url: str | None = None
    tracks: list[Track] = field(default_factory=list)
    # Numero di tracce dichiarato dal provider, se lo conosce (può differire
    # da len(tracks) quando le tracce non si possono leggere).
    track_count: int | None = None
    # Valorizzato quando le tracce non si possono leggere: mai una lista vuota
    # senza motivo.
    tracks_unavailable: TracksUnavailable | None = None
    # Opaque version marker the provider gives the playlist's content: it
    # changes when the playlist does (a snapshot id, an etag). The host stores
    # it to notice a followed playlist changed without comparing every track
    # (``MetadataProvider.get_playlist_snapshot``).
    snapshot_id: str | None = None
