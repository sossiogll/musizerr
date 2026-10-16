"""
Where a track's file goes in the library: the configured ``track_format``
(``str.format`` syntax, see CLAUDE.md) filled with the track's metadata, plus
the extension of the file. Used when a download is stored
(``workers/tasks.py``) and when the library file reconciliation looks for a
file under the current naming (``services/library_reconcile.py``).
"""
from __future__ import annotations

import re

from musizerr_sdk.models import Track

# Il segnaposto del numero di traccia (con eventuale formato, es. ":02d") e
# il separatore che lo segue ("." "-" "_" o spazi).
_TRACK_NUMBER_FIELD_RE = re.compile(r"\{track_number(?::[^}]*)?\}[\s._-]*")


def library_path(track: Track, track_format: str, suffix: str) -> str:
    """Path relativo in libreria: il formato configurato più l'estensione del
    file scaricato. Con il numero di traccia sconosciuto il segnaposto
    ``{track_number...}`` e il separatore che lo segue spariscono (es.
    "{track_number:02d}. {title}" diventa "{title}"), mai un "00". Il
    risultato resta sempre dentro la libreria, qualunque cosa contengano i
    metadati o il formato."""
    fmt = track_format
    if track.track_number is None:
        fmt = _TRACK_NUMBER_FIELD_RE.sub("", fmt)
    try:
        relative = fmt.format(
            artist=_sanitize(track.artist),
            album=_sanitize(track.album),
            title=_sanitize(track.title),
            track_number=track.track_number,
            release_year=track.year or "Unknown",
        )
    except (KeyError, ValueError, IndexError):
        relative = f"{_sanitize(track.artist)}/{_sanitize(track.album)}/{_sanitize(track.title)}"
    return _contained(relative) + suffix


def _sanitize(name: str | None) -> str:
    """Un valore dei metadati come nome di file o cartella: caratteri non
    validi sostituiti, mai vuoto né "." / ".."."""
    invalid = r'\/:*?"<>|'
    cleaned = "".join(c if c not in invalid else "_" for c in name or "").strip()
    return "Unknown" if cleaned in ("", ".", "..") else cleaned


def _contained(relative: str) -> str:
    """``relative`` senza componenti vuoti, "." o "..": niente "/" iniziale
    né risalite, quindi resta sotto la radice della libreria."""
    parts = [part.strip() for part in relative.split("/")]
    return "/".join("_" if part in (".", "..") else part for part in parts if part) or "Unknown"
