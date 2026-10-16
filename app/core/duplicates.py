"""
Dedup di libreria — logica pura, provider-agnostica (Golden Rule, CLAUDE.md).

Tre responsabilità, nessuna delle quali tocca DB/provider concreti:

1. ``cluster_library_items``: raggruppa i ``LibraryItem`` che sono
   verosimilmente la STESSA registrazione, riusando il matcher universale
   (``core.matching.match_track``) — stessa policy di tutta l'app: titolo +
   artisti che si intersecano, album/anno diversi = release diversa, non un
   duplicato. Un cluster è solo un candidato: la conferma reale è per
   fingerprint (fuori da qui, richiede leggere i byte).

2. ``quality_from_library_item``: qualità nota da tag/info tecniche già
   riportate dal sync provider (nessun probe sui byte) — None se il sync
   provider non ha riportato un codec riconoscibile (qualità sconosciuta).

3. ``decide_keeper``: quale copia tenere in un gruppo CONFERMATO (stesso
   recording id via fingerprint), o ``needs_review`` se la qualità di
   qualcuno è sconosciuta o se il "vincitore" implicherebbe cancellare un
   file che musizerr non ha mai messo lì (origine esterna/sconosciuta).
"""

from __future__ import annotations

from dataclasses import dataclass

from .audio_quality import QualityScore
from musizerr_sdk.text import LOSSLESS_CODECS
from .matching import match_track, title_identity_key
from musizerr_sdk.models import LibraryItem


def normalize_library_path(path: str) -> str:
    """
    Normalizza un path relativo per confrontare "lo stesso file" fra la
    libreria indicizzata dal sync provider (``LibraryItem.path``) e i path
    salvati da musizerr (``Download.file_path``, ``LibraryEntry.file_path``):
    separatori uniformi, niente prefisso "./" o "/" iniziale. Non fa
    case-folding — due path che differiscono solo per maiuscole/minuscole
    restano distinti (dipende dal filesystem, non lo assumiamo qui).
    """
    normalized = (path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def quality_from_library_item(item: LibraryItem) -> QualityScore | None:
    """
    Qualità dedotta dai tag/info tecniche già riportati dal sync provider
    (mai un probe sui byte). None se il codec non è riconoscibile — quello è
    "qualità sconosciuta", mai un default ottimistico o pessimistico.
    """
    suffix = (item.suffix or "").strip().lower()
    if not suffix:
        return None
    return QualityScore(
        lossless=suffix in LOSSLESS_CODECS,
        bitrate=item.bitrate or 0,
        sample_rate=item.sample_rate or 0,
        codec=suffix,
    )


def cluster_library_items(items: list[LibraryItem]) -> list[list[LibraryItem]]:
    """
    Raggruppa i candidati duplicati probabili. Bucket per identità del titolo
    (titolo base E versione: due remix della stessa canzone non sono
    duplicati, ``core.matching.title_identity_key``) prima del confronto
    fine: resta economico anche su librerie grandi (il confronto pairwise via
    ``match_track`` avviene solo DENTRO ciascun bucket, mai sull'intera
    libreria). Solo i cluster con più di un elemento sono duplicati candidati.
    """
    buckets: dict[str, list[LibraryItem]] = {}
    for item in items:
        key = title_identity_key(item.title or "")
        if not key:
            continue
        buckets.setdefault(key, []).append(item)

    clusters: list[list[LibraryItem]] = []
    for bucket_items in buckets.values():
        if len(bucket_items) < 2:
            continue
        clusters.extend(_cluster_bucket(bucket_items))
    return [c for c in clusters if len(c) > 1]


def _cluster_bucket(bucket_items: list[LibraryItem]) -> list[list[LibraryItem]]:
    """Union semplice per trascinamento all'interno di un bucket già ristretto
    per titolo: O(n²) ma n è la dimensione del bucket, non della libreria."""
    tracks = [item.to_track() for item in bucket_items]
    assigned = [False] * len(bucket_items)
    clusters: list[list[LibraryItem]] = []

    for i in range(len(bucket_items)):
        if assigned[i]:
            continue
        group = [bucket_items[i]]
        assigned[i] = True
        for j in range(i + 1, len(bucket_items)):
            if assigned[j]:
                continue
            if match_track(tracks[i], tracks[j]).matched:
                group.append(bucket_items[j])
                assigned[j] = True
        clusters.append(group)
    return clusters


# ------------------------------------------------------------------
# Keeper policy — decisione pura su un gruppo GIA' confermato per identità
# reale (fingerprint), mai su una semplice omonimia di titolo/artista.
# ------------------------------------------------------------------

PROPOSED = "proposed"
NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class DuplicateCandidate:
    """Un membro di un gruppo di duplicati confermato — input puro per la
    policy keeper, nessuna dipendenza da DB/provider."""

    key: str  # id stabile del membro per il chiamante (il sync id: mai il path, virtuale)
    quality: QualityScore | None  # None = sconosciuta
    deletable: bool  # True solo se il file è stato piazzato in libreria da musizerr


@dataclass(frozen=True)
class KeeperDecision:
    keeper_key: str | None
    status: str  # PROPOSED | NEEDS_REVIEW
    reason: str


def decide_keeper(
    candidates: list[DuplicateCandidate], *, prefer_lossless: bool = True,
) -> KeeperDecision:
    """
    Sceglie quale copia tenere in un gruppo CONFERMATO (stesso recording id
    via fingerprint, già filtrato dal chiamante). Mai un keeper se:

    - la qualità di ALMENO UN membro è sconosciuta (non si può confrontare
      con certezza) → needs_review, nessun keeper;
    - il "vincitore" per qualità implicherebbe cancellare un file NON
      cancellabile da musizerr (origine esterna o sconosciuta) → needs_review,
      nessun keeper (l'admin decide a mano, vedi CLAUDE.md D4).

    A parità totale di qualità, il pareggio è rotto in modo deterministico
    per ``key`` (ordine stabile fra run diversi sullo stesso input).
    """
    if not candidates:
        return KeeperDecision(None, NEEDS_REVIEW, "empty_group")
    if any(c.quality is None for c in candidates):
        return KeeperDecision(None, NEEDS_REVIEW, "unknown_quality")

    keeper = _ranked_by_quality(candidates, prefer_lossless)[0]

    non_deletable_losers = [c for c in candidates if c.key != keeper.key and not c.deletable]
    if non_deletable_losers:
        return KeeperDecision(None, NEEDS_REVIEW, "external_or_unknown_origin_would_be_deleted")

    return KeeperDecision(keeper.key, PROPOSED, "best_quality")


def _ranked_by_quality(candidates: list[DuplicateCandidate], prefer_lossless: bool) -> list[DuplicateCandidate]:
    """Best quality first; a full tie broken by ``key`` (stable across runs).
    Every quality must be known."""
    return sorted(
        candidates,
        key=lambda c: (c.quality.as_tuple(prefer_lossless), c.key),
        reverse=True,
    )


def best_quality_key(
    candidates: list[DuplicateCandidate], *, prefer_lossless: bool = True,
) -> str | None:
    """The member of a group whose quality beats every other one — a hint for
    the admin choosing by hand, never a choice: it ignores whether the others
    could be deleted. None when a quality is unknown or the best ones tie
    (nothing to say for sure)."""
    if len(candidates) < 2 or any(c.quality is None for c in candidates):
        return None
    first, second = _ranked_by_quality(candidates, prefer_lossless)[:2]
    if first.quality.as_tuple(prefer_lossless) == second.quality.as_tuple(prefer_lossless):
        return None
    return first.key
