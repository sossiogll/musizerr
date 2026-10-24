"""
Generic types for reconciling delegated downloads.

When a DownloadProvider hands a download over to an external system (e.g. a
library manager that downloads whole albums), Musizeer must be able to learn
its state and the files it produced without knowing which system it is. These
types are the common language: no provider name, no API-specific detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The states a ``DelegationStatus`` can report: the part of the host's canonical
# download vocabulary a delegate can answer with (the host adds its own, e.g.
# the waiting ones, and builds its vocabulary from these values).
DOWNLOADING = "downloading"   # audio retrieval in progress (on the external system)
IMPORTING = "importing"       # landing in the library (the delegate's import)
CONFLICT = "conflict"         # track already present, to be resolved
DISPATCHED = "dispatched"     # handed to the external downloader, nothing active seen
DOWNLOADED = "downloaded"     # terminal, OK
FAILED = "failed"             # terminal, KO


@dataclass
class FileRef:
    """
    Riferimento a un file gestito da un sistema esterno, con i metadati di
    qualità sufficienti al dedup *senza* leggere i byte (host-independent).
    """
    path: str
    quality_lossless: bool = False
    bitrate: int = 0
    sample_rate: int = 0
    external_id: str | None = None  # id opaco del provider (per delete via API)


@dataclass
class ImportConflict:
    """Una traccia che il sistema esterno segnala come già presente."""
    track_key: str                       # identità (titolo/mbid normalizzato)
    incoming: FileRef | None = None      # file appena scaricato dal delegato
    existing: FileRef | None = None      # copia già in libreria


@dataclass
class DelegationStatus:
    """
    Stato di un download delegato, espresso nel vocabolario canonico degli
    stati di download (``state`` = downloading | importing | conflict |
    dispatched | downloaded | failed: le costanti di questo modulo).
    """
    state: str
    conflicts: list[ImportConflict] = field(default_factory=list)
    detail: str | None = None
