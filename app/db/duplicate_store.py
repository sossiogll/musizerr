"""
Accesso alle tabelle del dedup di libreria (``duplicate_groups``,
``duplicate_members``, ``duplicate_fingerprint_cache`` — vedi
``db/models.py``). PURA data-access + orchestrazione dell'upsert idempotente:
la POLICY (clustering, scelta del keeper) resta in ``core.duplicates``,
provider-agnostica; questo modulo la applica al DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from core import download_states as ds
from core.audio_quality import QualityScore
from core.duplicates import DuplicateCandidate, decide_keeper
from core.identifiers import sync_id_type
from musizeer_sdk.interfaces import Identity
from core.matching import match_track, title_identity_key
from musizeer_sdk.models import Track

from .expressions import descending
from .models import Download, DuplicateFingerprintCache, DuplicateGroup, DuplicateMember, TrackIdentifierRecord, TrackRecord
from .track_store import track_from_record
from .upsert import upsert

ACTIVE_STATUSES = ("proposed", "needs_review")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------
# Fingerprint cache — chiave (sync_id, size). Colonna DB storica si chiama
# ``path`` (vedi db/models.py::DuplicateFingerprintCache) ma dalla revisione
# "fetch per sync_id" contiene il sync_id, mai un path — e' solo una chiave
# di cache interna, mai esposta: non vale una migrazione per rinominarla.
# ------------------------------------------------------------------

def get_cached_fingerprint(session: Session, sync_id: str, size: int) -> tuple[bool, Identity | None]:
    """
    Esito cachato per (sync_id, size): ``(hit, identity)``. ``hit=False``
    significa "nessuna riga in cache, va fingerprintato adesso" — diverso da
    ``hit=True, identity=None`` ("già fingerprintato, non identificabile":
    un hit legittimo, mai ritentato a ogni run)."""
    row = session.exec(
        select(DuplicateFingerprintCache).where(
            DuplicateFingerprintCache.path == sync_id,
            DuplicateFingerprintCache.size == size,
        )
    ).first()
    if row is None:
        return False, None
    if row.recording_id is None:
        return True, None
    return True, Identity(
        title=row.title or "", artist=row.artist or "",
        score=row.score or 0.0, recording_id=row.recording_id,
    )


def save_fingerprint_cache(session: Session, sync_id: str, size: int, identity: Identity | None) -> None:
    upsert(
        session,
        DuplicateFingerprintCache(
            path=sync_id, size=size,
            recording_id=identity.recording_id if identity else None,
            title=identity.title if identity else None,
            artist=identity.artist if identity else None,
            score=identity.score if identity else None,
            computed_at=_now(),
        ),
        conflict=("path", "size"),
        update=("recording_id", "title", "artist", "score", "computed_at"),
    )


# ------------------------------------------------------------------
# Origine — quali item della libreria musizeer ha davvero piazzato lei
# stessa, risalendo per SYNC ID (mai per path: il path riportato dal sync
# provider e' spesso virtuale/ricostruito dai tag, non affidabile per un
# confronto — vedi musizeer_sdk.models.LibraryItem).
# ------------------------------------------------------------------

def build_downloaded_track_index(session: Session) -> dict[str, list[tuple[Download, Track]]]:
    """
    Indice (bucket per titolo normalizzato, stesso schema di
    ``core.duplicates.cluster_library_items``) di ogni ``Download``
    'downloaded' con la sua Track di dominio — costruito UNA VOLTA per scan
    e riusato dal fallback di ``resolve_download_for_item`` (mai una query/
    match per membro)."""
    rows = session.exec(
        select(Download, TrackRecord)
        .join(TrackRecord, Download.track_id == TrackRecord.internal_id)
        .where(Download.status == ds.DOWNLOADED, Download.file_path.is_not(None))
    ).all()
    index: dict[str, list[tuple[Download, Track]]] = {}
    for download, record in rows:
        track = track_from_record(record)
        key = title_identity_key(track.title or "")
        index.setdefault(key, []).append((download, track))
    return index


def resolve_download_for_item(
    session: Session, *, sync_name: str, item, downloaded_index: dict[str, list[tuple[Download, Track]]],
) -> tuple[Download | None, str]:
    """
    Trova il ``Download`` musizeer per un ``LibraryItem``, in due passi:

    1. Link ESATTO per sync id, via ``track_identifiers`` (id_type =
       ``sync_id_type(sync_name)``, valore = ``item.sync_id``) -> track_id ->
       ``Download`` 'downloaded'. Affidabile al 100%, ma popolato solo se
       qualcuno ha già usato lo streaming musizeer per quella traccia (vedi
       ``services/stream.py::resolve_sync_id``, che lo scrive lazy) — non ogni
       download musizeer ce l'ha.
    2. Fallback via il matcher universale (``core.matching.match_track``)
       contro ogni ``Download`` 'downloaded' noto, quando il link diretto
       manca — STESSA policy di match di tutta l'app (titolo/artisti/album/
       anno), mai un'euristica bespoke, e mai sul path (virtuale, vedi sopra).

    Ritorna ``(download_o_None, metodo)`` con ``metodo`` in
    ``"sync_id" | "match" | "none"`` — usato solo per report/diagnostica,
    mai persistito (l'unico dato persistito è ``download_id``, invariato
    rispetto a prima)."""
    identifier = session.exec(
        select(TrackIdentifierRecord).where(
            TrackIdentifierRecord.id_type == sync_id_type(sync_name),
            TrackIdentifierRecord.value == item.sync_id,
        )
    ).first()
    if identifier is not None:
        download = session.exec(
            select(Download)
            .where(
                Download.track_id == identifier.track_id,
                Download.status == ds.DOWNLOADED,
                Download.file_path.is_not(None),
            )
            .order_by(descending(Download.completed_at))
        ).first()
        if download is not None:
            return download, "sync_id"

    item_track = item.to_track()
    bucket_key = title_identity_key(item.title or "")
    for download, track in downloaded_index.get(bucket_key, []):
        if match_track(item_track, track).matched:
            return download, "match"
    return None, "none"


# ------------------------------------------------------------------
# Upsert idempotente di un gruppo per recording_id
# ------------------------------------------------------------------

@dataclass
class MemberInput:
    sync_id: str
    path: str
    title: str
    artist: str
    album: str
    year: int | None
    duration_ms: int
    size: int
    suffix: str
    bitrate: int
    sample_rate: int
    quality: QualityScore | None
    fingerprint_score: float | None
    download_id: str | None  # None = non piazzato da musizeer, mai cancellabile


def _member_key(member: MemberInput, index: int) -> str:
    """What identifies a member for the keeper policy: its sync id, one per
    file. Never its path: the path a sync provider reports is virtual
    (rebuilt from the tags), two files can share it, and keyed by path both
    were "the keeper" of a group that deleted nothing."""
    return member.sync_id or f"member:{index}"


def upsert_group_for_recording(
    session: Session, *, recording_id: str, members: list[MemberInput], prefer_lossless: bool,
) -> tuple[DuplicateGroup, bool]:
    """
    Trova (o crea) il gruppo NON risolto per ``recording_id`` e ne ricrea i
    membri dai dati appena raccolti — idempotente: uno stesso recording_id
    processato due volte nello stesso o in run successivi aggiorna la STESSA
    riga, mai una seconda. Un gruppo già 'resolved'/'dismissed' non viene mai
    toccato (resta storico); se non ce n'è uno attivo, se ne crea uno nuovo.
    Ritorna (gruppo, creato_adesso).
    """
    existing = session.exec(
        select(DuplicateGroup).where(
            DuplicateGroup.recording_id == recording_id,
            DuplicateGroup.status.in_(ACTIVE_STATUSES),
        )
    ).first()

    created = existing is None
    group = existing or DuplicateGroup(recording_id=recording_id)
    if created:
        session.add(group)
        session.flush()
    else:
        for old_member in session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == group.id)
        ).all():
            session.delete(old_member)
        session.flush()

    candidates: list[DuplicateCandidate] = []
    member_rows: list[DuplicateMember] = []
    for m in members:
        deletable = m.download_id is not None
        row = DuplicateMember(
            group_id=group.id,
            sync_id=m.sync_id,
            path=m.path,
            title=m.title,
            artist=m.artist,
            album=m.album,
            year=m.year,
            duration_ms=m.duration_ms,
            size=m.size,
            suffix=m.suffix,
            bitrate=m.bitrate,
            sample_rate=m.sample_rate,
            lossless=m.quality.lossless if m.quality else None,
            quality_known=m.quality is not None,
            fingerprint_score=m.fingerprint_score,
            origin="musizeer" if deletable else "unknown",
            download_id=m.download_id,
            is_keeper=False,
        )
        session.add(row)
        member_rows.append(row)
        candidates.append(DuplicateCandidate(key=_member_key(m, len(candidates)), quality=m.quality, deletable=deletable))
    session.flush()

    decision = decide_keeper(candidates, prefer_lossless=prefer_lossless)
    group.status = decision.status
    group.reason = decision.reason
    group.updated_at = _now()
    if decision.keeper_key is not None:
        for candidate, row in zip(candidates, member_rows):
            row.is_keeper = candidate.key == decision.keeper_key
            session.add(row)
    session.add(group)

    return group, created


def drop_missing_members(session: Session, existing_paths: set[str]) -> int:
    """
    Rimuove dai gruppi ANCORA attivi (proposed/needs_review) i membri il cui
    path non compare più nella libreria appena elencata (file spostato/
    cancellato al di fuori del dedup) — mai per un gruppo già risolto/
    dismesso (storico). Se un gruppo scende sotto 2 membri non è più un
    duplicato: viene dismesso automaticamente.
    """
    dropped = 0
    groups = session.exec(
        select(DuplicateGroup).where(DuplicateGroup.status.in_(ACTIVE_STATUSES))
    ).all()
    for group in groups:
        member_rows = session.exec(
            select(DuplicateMember).where(DuplicateMember.group_id == group.id)
        ).all()
        stale = [m for m in member_rows if m.path not in existing_paths]
        if not stale:
            continue
        for m in stale:
            session.delete(m)
            dropped += 1
        if len(member_rows) - len(stale) < 2:
            group.status = "dismissed"
            group.reason = "member_file_missing"
            group.updated_at = _now()
            session.add(group)
    return dropped


# ------------------------------------------------------------------
# Lettura per l'API di risoluzione
# ------------------------------------------------------------------

def list_groups(session: Session, status: str | None = None) -> list[DuplicateGroup]:
    query = select(DuplicateGroup)
    if status:
        query = query.where(DuplicateGroup.status == status)
    return session.exec(query.order_by(DuplicateGroup.created_at.desc())).all()


def get_group_members(session: Session, group_id: str) -> list[DuplicateMember]:
    return session.exec(
        select(DuplicateMember).where(DuplicateMember.group_id == group_id)
    ).all()


def member_quality(member: DuplicateMember) -> QualityScore | None:
    """The quality the scan recorded for a member (None = unknown), as the
    keeper policy compares it."""
    if not member.quality_known:
        return None
    return QualityScore(
        lossless=bool(member.lossless), bitrate=member.bitrate or 0,
        sample_rate=member.sample_rate or 0, codec=member.suffix or "",
    )


def member_downloads(session: Session, members: list[DuplicateMember]) -> dict[str, Download]:
    """The Musizeer download each member is linked to, by member id: where
    its file really is on the storage (the member's own path is the sync
    provider's label, often virtual — see ``musizeer_sdk.models.LibraryItem``)."""
    ids = {m.download_id for m in members if m.download_id}
    if not ids:
        return {}
    downloads = {d.id: d for d in session.exec(select(Download).where(Download.id.in_(ids))).all()}
    return {m.id: downloads[m.download_id] for m in members if m.download_id in downloads}
