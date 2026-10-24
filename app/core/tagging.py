"""
Lettura e scrittura tag audio, generazione di file di prova.

Operazioni locali deterministiche → utility di ``core/``, non provider.
La scrittura tag usa mutagen (già dipendenza); la generazione del probe usa
ffmpeg (già presente).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from musizeer_sdk.identifiers import ISRC, MBID, Identifier
from musizeer_sdk.models import Track
from musizeer_sdk.text import parse_year

logger = logging.getLogger(__name__)

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def read_track_tags(path: str) -> Track | None:
    """
    La Track descritta dai tag del file: titolo, artista, album, durata, anno,
    numero di traccia e di disco, ISRC e MBID della registrazione. None se il
    formato non è leggibile o mancano titolo o artista, gli unici dati da cui
    si può riconoscere una traccia senza identificativi.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Lettura tag fallita su %s: %s", path, exc)
        return None
    if audio is None:
        return None

    title = _first_tag(audio, "title")
    artist = _first_tag(audio, "artist")
    if not title or not artist:
        return None

    track = Track(
        title=title,
        artist=artist,
        album=_first_tag(audio, "album") or "",
        duration_ms=int(audio.info.length * 1000) if getattr(audio, "info", None) else 0,
        year=parse_year(_first_tag(audio, "date")),
        track_number=_position(_first_tag(audio, "tracknumber")),
        disc_number=_position(_first_tag(audio, "discnumber")),
    )
    isrc = _first_tag(audio, "isrc")
    if isrc:
        track.add_id(Identifier(type=ISRC, value=isrc.upper()))
    mbid = _first_tag(audio, "musicbrainz_trackid")
    if mbid:
        track.add_id(Identifier(type=MBID, value=mbid))
    return track


def _first_tag(audio, key: str) -> str | None:
    try:
        values = audio.get(key)
    except (KeyError, ValueError):
        return None
    if not values:
        return None
    return str(values[0]).strip() or None


def _position(value: str | None) -> int | None:
    """Il numero di un tag di posizione, scritto ``3`` o ``3/12``; None se
    manca o è 0."""
    if not value:
        return None
    try:
        number = int(value.split("/")[0])
    except ValueError:
        return None
    return number or None


def write_tags(
    path: str,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
) -> bool:
    """
    Scrive i tag indicati sul file (solo quelli non-None), preservando il resto.
    Ritorna True se salvato. Format-aware via mutagen (easy interface).
    """
    try:
        from mutagen import File as MutagenFile
    except Exception as exc:  # noqa: BLE001
        logger.error("mutagen non disponibile: %s", exc)
        return False

    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            logger.warning("Formato non riconosciuto da mutagen: %s", path)
            return False
        if audio.tags is None:
            audio.add_tags()
        if title is not None:
            audio["title"] = title
        if artist is not None:
            audio["artist"] = artist
        if album is not None:
            audio["album"] = album
        audio.save()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scrittura tag fallita su %s: %s", path, exc)
        return False


def make_probe_audio(
    title: str,
    artist: str,
    *,
    album: str | None = None,
    seconds: int = 1,
    path: str | None = None,
) -> str | None:
    """
    Genera un file .ogg minimo valido (silenzio) con i tag indicati.
    Usato dal probe del full-sync mode. Ritorna il path o None su errore.
    """
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
    try:
        meta = ["-metadata", f"title={title}", "-metadata", f"artist={artist}"]
        if album is not None:
            meta += ["-metadata", f"album={album}"]
        subprocess.run(
            [_FFMPEG, "-v", "error", "-y",
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
             "-t", str(seconds),
             *meta,
             "-c:a", "libvorbis", path],
            check=True, capture_output=True, timeout=30,
        )
        return path
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("make_probe_audio failed: %s", exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
