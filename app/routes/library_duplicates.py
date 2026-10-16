"""
API admin per la risoluzione dei duplicati di libreria proposti dal routine
``library_duplicates_scan`` (vedi ``workers/duplicates.py``). Il routine
PROPONE soltanto — ogni cancellazione reale passa da qui, sempre un'azione
esplicita dell'admin (CLAUDE.md: mai cancellare un file reale se non
richiesto).

Provider-agnostico: usa solo i contratti ``StorageProvider``/``SyncProvider``
via registry, mai un nome di provider concreto.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from core.duplicates import DuplicateCandidate, best_quality_key
from core.utc import utc_iso
from db.duplicate_store import get_group_members, list_groups, member_downloads, member_quality
from db.models import Download, DuplicateGroup, DuplicateMember, LibraryEntry
from routes.deps import AdminUserDep, AuthManagerDep, ConfigDep, RegistryDep, SessionDep
from services import public_urls
from services.storage import resolve_storage_provider

router = APIRouter()


class ResolveBody(BaseModel):
    keep_member_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _member_dict(m: DuplicateMember, download: Download | None, best_quality: bool) -> dict:
    """``path`` is the label the sync provider shows (often virtual, two
    files can share it); ``storage_path`` is where the file really is on the
    storage, when Musizerr knows it (the download it is linked to), with
    when it arrived and through which download provider."""
    return {
        "id": m.id,
        "sync_id": m.sync_id,
        "path": m.path,
        "storage_path": download.file_path if download else None,
        "added_at": utc_iso(download.completed_at) if download else None,
        "downloaded_via": download.provider_used if download else None,
        "title": m.title,
        "artist": m.artist,
        "album": m.album,
        "year": m.year,
        "duration_ms": m.duration_ms,
        "size": m.size,
        "suffix": m.suffix,
        "bitrate": m.bitrate,
        "sample_rate": m.sample_rate,
        "lossless": m.lossless,
        "quality_known": m.quality_known,
        "best_quality": best_quality,
        "fingerprint_score": m.fingerprint_score,
        "origin": m.origin,
        "deletable": m.origin == "musizerr" and bool(m.download_id),
        "is_keeper": m.is_keeper,
        "deleted_at": utc_iso(m.deleted_at),
        # A deleted file has nothing to play.
        "stream_url": None if m.deleted_at else public_urls.duplicate_member_stream_url(m.id),
    }


def _group_dict(session, group: DuplicateGroup, prefer_lossless: bool) -> dict:
    members = get_group_members(session, group.id)
    downloads = member_downloads(session, members)
    best = best_quality_key(
        [DuplicateCandidate(key=m.id, quality=member_quality(m), deletable=False) for m in members if not m.deleted_at],
        prefer_lossless=prefer_lossless,
    )
    return {
        "id": group.id,
        "recording_id": group.recording_id,
        "status": group.status,
        "reason": group.reason,
        "created_at": utc_iso(group.created_at),
        "updated_at": utc_iso(group.updated_at),
        "resolved_at": utc_iso(group.resolved_at),
        "deleted_paths": json.loads(group.deleted_paths) if group.deleted_paths else [],
        "members": [_member_dict(m, downloads.get(m.id), m.id == best) for m in members],
    }


@router.get("/library/duplicates")
def get_duplicate_groups(
    admin: AdminUserDep, session: SessionDep, config: ConfigDep, status: str | None = Query(None),
):
    groups = list_groups(session, status=status)
    return {"groups": [_group_dict(session, g, config.dedup_prefer_lossless) for g in groups]}


def _resolve_group(
    session, registry, config, auth_manager, group: DuplicateGroup, keep_member_id: str,
) -> dict:
    """
    Cancella (via StorageProvider) ogni membro del gruppo diverso dal keeper
    scelto — MA solo quelli di origine ``musizerr`` (``deletable``) E con un
    ``download_id`` risolto: un membro esterno/sconosciuto non viene mai
    cancellato, resta semplicemente "saltato" nella risposta (mai un errore
    che blocca l'intera risoluzione: proteggere quel file è il comportamento
    corretto, non un fallimento).

    ATTENZIONE: la cancellazione usa SEMPRE il path REALE letto da
    ``Download.file_path`` (via ``m.download_id``), MAI ``m.path`` — quello è
    solo l'etichetta virtuale che il sync provider mostra (vedi
    ``musizerr_sdk.models.LibraryItem``), spesso diversa dalla posizione vera sullo
    storage.

    Ogni cancellazione riuscita si registra subito: il download non punta
    più a quel file e la voce di libreria sparisce, così né lo stream né lo
    stato di download lo danno per presente. Se una cancellazione fallisce,
    quelle già fatte restano registrate e un nuovo tentativo le salta.
    """
    if group.status not in ("proposed", "needs_review"):
        raise HTTPException(status_code=409, detail=f"group already {group.status}")

    members = get_group_members(session, group.id)
    keeper = next((m for m in members if m.id == keep_member_id), None)
    if keeper is None:
        raise HTTPException(status_code=400, detail="keep_member_id not in this group")

    storage, _name = resolve_storage_provider(registry, config.storage_provider)
    if storage is None:
        raise HTTPException(status_code=503, detail="no storage provider available")
    storage_session = auth_manager.authenticate(storage)

    deleted_paths: list[str] = json.loads(group.deleted_paths) if group.deleted_paths else []
    skipped_paths: list[str] = []
    for m in members:
        if m.id == keeper.id or m.deleted_at is not None:
            continue
        if m.origin != "musizerr" or not m.download_id:
            skipped_paths.append(m.path)
            continue
        download = session.get(Download, m.download_id)
        if download is None or not download.file_path:
            skipped_paths.append(m.path)
            continue
        real_path = download.file_path
        try:
            storage.delete(real_path, storage_session)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"cancellazione fallita per '{real_path}': {exc}",
            ) from exc
        m.deleted_at = _now()
        download.file_path = None
        for entry in session.exec(select(LibraryEntry).where(LibraryEntry.file_path == real_path)).all():
            session.delete(entry)
        deleted_paths.append(real_path)
        group.deleted_paths = json.dumps(deleted_paths)
        session.add_all([m, download, group])
        session.commit()

    keeper.is_keeper = True
    session.add(keeper)

    group.status = "resolved"
    group.resolved_at = _now()
    group.updated_at = group.resolved_at
    group.deleted_paths = json.dumps(deleted_paths)
    session.add(group)
    session.commit()

    return {
        "group_id": group.id,
        "status": group.status,
        "deleted": deleted_paths,
        "skipped_non_deletable": skipped_paths,
    }


def _trigger_rescan(registry, config, auth_manager) -> None:
    sync_name = config.sync_provider
    if not sync_name:
        return
    sync = registry.get_sync_provider(sync_name)
    if sync is None:
        return
    try:
        sync_session = auth_manager.authenticate(sync)
        sync.trigger_scan(sync_session)
    except Exception:  # noqa: BLE001 — rescan best-effort: la cancellazione e' gia' avvenuta
        pass


@router.post("/library/duplicates/{group_id}/resolve")
def resolve_duplicate_group(
    group_id: str, body: ResolveBody, admin: AdminUserDep, session: SessionDep,
    registry: RegistryDep, config: ConfigDep, auth_manager: AuthManagerDep,
):
    group = session.get(DuplicateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    result = _resolve_group(session, registry, config, auth_manager, group, body.keep_member_id)
    if result["deleted"]:
        _trigger_rescan(registry, config, auth_manager)
    return result


@router.post("/library/duplicates/resolve-all")
def resolve_all_duplicate_groups(
    admin: AdminUserDep, session: SessionDep, registry: RegistryDep, config: ConfigDep, auth_manager: AuthManagerDep,
):
    """Risolve OGNI gruppo 'proposed' (mai 'needs_review': per quelli manca
    un keeper automatico, l'admin deve scegliere a mano via /resolve)."""
    results = []
    any_deleted = False
    for group in list_groups(session, status="proposed"):
        members = get_group_members(session, group.id)
        keeper = next((m for m in members if m.is_keeper), None)
        if keeper is None:
            continue  # non dovrebbe succedere per 'proposed' (decide_keeper ne garantisce uno)
        result = _resolve_group(session, registry, config, auth_manager, group, keeper.id)
        results.append(result)
        any_deleted = any_deleted or bool(result["deleted"])

    if any_deleted:
        _trigger_rescan(registry, config, auth_manager)
    return {"resolved": results}


@router.post("/library/duplicates/{group_id}/dismiss")
def dismiss_duplicate_group(group_id: str, admin: AdminUserDep, session: SessionDep):
    group = session.get(DuplicateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    if group.status not in ("proposed", "needs_review"):
        raise HTTPException(status_code=409, detail=f"group already {group.status}")
    group.status = "dismissed"
    group.updated_at = _now()
    session.add(group)
    session.commit()
    return {"group_id": group.id, "status": group.status}
