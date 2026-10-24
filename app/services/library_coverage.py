"""
Copertura della libreria per la discografia di un artista (e le schede della
Home): quante tracce di ogni uscita sono in libreria, in corso o fallite, con
la stessa definizione di "in libreria" della riga di una traccia
(``db.library_store``: download arrivato, file sullo storage o traccia
indicizzata dal sync provider) e lo stesso conteggio della pagina dell'album
(``core.download_states.song_coverage``). Le voci della
discografia di solito non portano le tracce, quindi il confronto avviene
fra le tracce richieste in libreria e le edizioni dell'uscita, col matching
di core (``match_release`` sull'album di ogni traccia): nessuna chiamata ai
provider.

Le tracce in libreria si raggruppano una volta sola per titolo base
dell'album; ogni uscita guarda solo il gruppo dei propri titoli, e solo
quelle tracce vengono lette per intero (anno) e confrontate. Una canzone
conta una volta per identità del titolo (titolo base E versione,
``core.matching.title_identity_key``): la stessa canzone in due edizioni è
una, due remix della stessa canzone sono due.

Il totale di un'uscita è il numero di tracce che un catalogo le dà; mai
quello di un sync provider, che conta i file che la libreria ne ha. Se
nessuno lo sa resta ``None`` ("N in libreria").
"""
from __future__ import annotations

from sqlmodel import Session, select

from core import download_states as ds
from core.discography import ReleaseGroup
from core.matching import base_title_key, match_release, title_identity_key
from musizeer_sdk.models import Album
from db.library_store import all_file_states
from db.models import TrackRecord
from db.track_store import track_from_record
from services.track_identity import sync_provider_names
from services.track_state import sync_id_types


class _Library:
    """Le tracce con uno stato file (richieste o in libreria), indicizzate
    per titolo base dell'album."""

    def __init__(self, session: Session, registry) -> None:
        self._session = session
        self._status = all_file_states(session, sync_id_types(registry))
        self._by_album: dict[str, list[tuple[str, str]]] = {}
        if self._status:
            rows = [
                row for row in session.exec(select(TrackRecord.internal_id, TrackRecord.title, TrackRecord.album)).all()
                if row.internal_id in self._status
            ]
            for row in rows:
                if row.album:
                    self._by_album.setdefault(base_title_key(row.album), []).append((row.internal_id, row.title))
        self._albums: dict[str, Album] = {}

    def candidates(self, group: ReleaseGroup) -> list[tuple[str, str]]:
        keys = {base_title_key(edition.title) for edition in group.editions}
        return [candidate for key in keys for candidate in self._by_album.get(key, [])]

    def album_of(self, track_ids: list[str]) -> dict[str, Album]:
        """L'album di ogni traccia (titolo, artisti, anno), letto per intero
        solo la prima volta che serve."""
        missing = [track_id for track_id in track_ids if track_id not in self._albums]
        if missing:
            for record in self._session.exec(select(TrackRecord).where(TrackRecord.internal_id.in_(missing))).all():
                track = track_from_record(record)
                self._albums[record.internal_id] = Album(
                    title=track.album, artist=track.artist, artists=track.artists, year=track.year,
                )
        return {track_id: self._albums[track_id] for track_id in track_ids if track_id in self._albums}

    def status(self, track_id: str) -> str:
        return self._status[track_id]


def release_total(group: ReleaseGroup, sync_names: set[str]) -> int | None:
    """Le tracce dell'uscita secondo i cataloghi (la più ampia delle sue
    edizioni), ``None`` se nessun catalogo lo sa."""
    counts = [e.track_count for e in group.editions if e.track_count and e.provider not in sync_names]
    return max(counts, default=None)


def release_coverage(groups: list[ReleaseGroup], session: Session, registry) -> list[dict[str, int | None]]:
    """Una copertura per gruppo, nello stesso ordine. La stessa canzone in
    più edizioni dell'uscita (es. standard e deluxe) conta una volta sola,
    col suo stato migliore."""
    library = _Library(session, registry)
    sync_names = sync_provider_names(registry)
    result = []
    for group in groups:
        candidates = library.candidates(group)
        albums = library.album_of([track_id for track_id, _title in candidates])
        songs = [
            (title_identity_key(title), library.status(track_id))
            for track_id, title in candidates
            if (album := albums.get(track_id)) is not None
            and any(match_release(album, edition).matched for edition in group.editions)
        ]
        result.append(ds.song_coverage(songs, total=release_total(group, sync_names)))
    return result


def summarize(coverages: list[dict[str, int | None]]) -> dict[str, int | None]:
    """Il riepilogo di più uscite; il totale è ``None`` se quello di una di
    esse non è noto."""
    totals = [c["total"] for c in coverages]
    summary: dict[str, int | None] = {key: sum(c[key] for c in coverages) for key in ("downloaded", "in_progress", "failed")}
    summary["total"] = None if any(t is None for t in totals) else sum(totals)
    return summary
