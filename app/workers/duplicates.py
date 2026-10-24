"""
Routine "library_duplicates_scan" (vedi ``core/routines.py``): scopre
duplicati di libreria a costo contenuto — candidati via
``SyncProvider.list_library`` (nessun download), clustering col matcher
universale (``core.matching``, tramite ``core.duplicates``), conferma reale
per fingerprint audio (``IdentificationProvider``, solo sui candidati
clusterizzati, i byte via ``SyncProvider.fetch_partial`` — MAI seguendo il
path di ``list_library``, che e' spesso virtuale/ricostruito dai tag e non
punta al file reale, vedi ``musizeer_sdk.models.LibraryItem``) — poi PROPONE gruppi
con keeper per qualità.

Non cancella MAI nulla da sola: la risoluzione (cancellazione dei membri non
keeper) è sempre un'azione admin esplicita, vedi
``routes/library_duplicates.py`` — che risolve l'origine/cancellabilità per
SYNC ID (``db.duplicate_store.resolve_download_for_item``), mai per path.

Gate generici (mai un nome di provider qui, Golden Rule): ``dedup_enabled``
(la deduplicazione di libreria è voluta) e ``identification_enabled`` (l'app
può fare identificazione via fingerprint) — stesso schema di
``workers.tasks._should_run_anti_mislabel``.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from core.duplicates import cluster_library_items, normalize_library_path, quality_from_library_item
from db.duplicate_store import (
    MemberInput,
    build_downloaded_track_index,
    drop_missing_members,
    get_cached_fingerprint,
    resolve_download_for_item,
    save_fingerprint_cache,
    upsert_group_for_recording,
)
from db.engine import engine
from services.auth import auth_manager_for
from services.credentials import shared_store
from services.identification import confident
from services.workspace import open_workspace

from .celery_app import celery, get_config, get_registry

logger = logging.getLogger(__name__)

# Un fingerprint audio analizza solo i primi ~120s (fpcalc/Chromaprint): il
# sync provider puo' troncare il download a questo tanto invece di mandare
# il file intero. 12MB coprono comodamente 120s anche a bitrate lossless
# generosi (~800kbps).
_FINGERPRINT_MAX_BYTES = 12 * 1024 * 1024


@celery.task(name="workers.duplicates.scan_library_duplicates")
def scan_library_duplicates() -> dict:
    config = get_config()
    registry = get_registry()

    if not config.dedup_enabled:
        return {"status": "skipped", "detail": "dedup_enabled disattivato"}
    if not config.identification_enabled:
        return {"status": "skipped", "detail": "identification_enabled disattivato"}

    sync_name = config.sync_provider
    if not sync_name:
        return {"status": "error", "detail": "nessun sync provider configurato"}
    sync = registry.get_sync_provider(sync_name)
    if sync is None:
        return {"status": "error", "detail": f"sync provider '{sync_name}' non trovato"}

    identification = next((p for p in registry.all_identification() if _is_available(p)), None)
    if identification is None:
        return {"status": "skipped", "detail": "nessun identification provider disponibile"}

    # Transazioni brevi: la scansione dura minuti (I/O di rete per ogni file)
    # e l'API scrive sullo stesso database. Si committa dopo ogni scrittura,
    # così nessun lock di scrittura resta aperto durante il fingerprint.
    # expire_on_commit=False: gli oggetti letti (indice dei download) restano
    # validi fra un commit e l'altro senza ricaricarli.
    with Session(engine, expire_on_commit=False) as session:
        store = shared_store(session)
        auth = auth_manager_for(store)
        try:
            sync_session = auth.authenticate(sync)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"autenticazione sync fallita: {exc}"}
        # L'autenticazione può aver rinnovato un token: chiude la scrittura
        # prima dell'I/O di rete.
        session.commit()

        try:
            items = sync.list_library(sync_session)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"list_library fallita: {exc}"}

        if not items:
            return {
                "status": "done", "candidates": 0, "clusters": 0,
                "fingerprinted": 0, "groups_created": 0, "groups_updated": 0, "dropped_members": 0,
                "linked_via_sync_id": 0, "linked_via_match": 0, "linked_none": 0,
            }

        # Path normalizzato UNA volta — SOLO come chiave d'identità stabile fra
        # run (per il drop dei membri spariti), mai per leggere byte o
        # decidere l'origine (vedi musizeer_sdk.models.LibraryItem).
        for item in items:
            item.path = normalize_library_path(item.path)

        clusters = cluster_library_items(items)
        downloaded_index = build_downloaded_track_index(session)

        fingerprinted = 0
        groups_created = 0
        groups_updated = 0
        link_counts = {"sync_id": 0, "match": 0, "none": 0}

        for cluster in clusters:
            by_recording: dict[str, list] = {}
            for item in cluster:
                if not item.sync_id:
                    continue
                # La cache tiene l'identità grezza del provider: la soglia
                # si applica a ogni lettura, quindi cambiarla vale anche per i
                # file già identificati. Un file non raggiunto non si mette in
                # cache: si riprova al prossimo scan.
                hit, identity = get_cached_fingerprint(session, item.sync_id, item.size)
                if not hit:
                    reached, identity = _fingerprint_item(item, sync, sync_session, identification, config)
                    fingerprinted += 1
                    if reached:
                        save_fingerprint_cache(session, item.sync_id, item.size, identity)
                        session.commit()
                identity = confident(identity, config)
                # Identità sconosciuta o senza recording id → mai un
                # duplicato "confermato" (CLAUDE.md: mai su identità incerta).
                if identity is None or not identity.recording_id:
                    continue
                by_recording.setdefault(identity.recording_id, []).append((item, identity))

            for recording_id, matched in by_recording.items():
                if len(matched) < 2:
                    continue
                resolved = []
                for matched_item, matched_identity in matched:
                    download, method = resolve_download_for_item(
                        session, sync_name=sync_name, item=matched_item, downloaded_index=downloaded_index,
                    )
                    link_counts[method] += 1
                    resolved.append((matched_item, matched_identity, download, method))

                # Sicurezza: piu' membri DIVERSI dello stesso gruppo possono
                # risolvere allo STESSO download_id — visto dal vivo sia via
                # fallback per tag (es. "Curre curre guaglió"/"guagliò",
                # troppo simili per distinguerli) SIA via link DIRETTO per
                # sync id (es. lo storico streaming ha associato due sync_id
                # diversi — due file fisici diversi, confermato dal
                # fingerprint — allo stesso track_id in momenti diversi).
                # ``Download.file_path`` e' UN SOLO path reale: il download
                # resta al membro con il legame piu' forte (il link esatto per
                # sync id batte il fallback per tag: "Wastelands", "Luna di
                # città d'agosto" restavano altrimenti senza file cancellabile);
                # a parita' di forza nessuno resta collegato, perche'
                # cancellare "il loser" potrebbe cancellare il file del keeper.
                members = []
                for matched_item, matched_identity, download, method in _settle_download_claims(resolved):
                    members.append(MemberInput(
                        sync_id=matched_item.sync_id,
                        path=matched_item.path,
                        title=matched_item.title,
                        artist=matched_item.artist,
                        album=matched_item.album,
                        year=matched_item.year,
                        duration_ms=matched_item.duration_ms,
                        size=matched_item.size,
                        suffix=matched_item.suffix,
                        bitrate=matched_item.bitrate,
                        sample_rate=matched_item.sample_rate,
                        quality=quality_from_library_item(matched_item),
                        fingerprint_score=matched_identity.score,
                        download_id=download.id if download else None,
                    ))
                _, created = upsert_group_for_recording(
                    session, recording_id=recording_id, members=members,
                    prefer_lossless=config.dedup_prefer_lossless,
                )
                session.commit()
                if created:
                    groups_created += 1
                else:
                    groups_updated += 1

        current_paths = {item.path for item in items if item.path}
        dropped = drop_missing_members(session, current_paths)

        session.commit()

    return {
        "status": "done",
        "candidates": len(items),
        "clusters": len(clusters),
        "fingerprinted": fingerprinted,
        "groups_created": groups_created,
        "groups_updated": groups_updated,
        "dropped_members": dropped,
        "linked_via_sync_id": link_counts["sync_id"],
        "linked_via_match": link_counts["match"],
        "linked_none": link_counts["none"],
    }


def _is_available(provider) -> bool:
    try:
        return bool(provider.is_available())
    except Exception:  # noqa: BLE001
        return False


def _fingerprint_item(item, sync, sync_session, identification, config):
    """
    Recupera una copia (parziale se il provider lo sa fare) del file via
    ``SyncProvider.fetch_partial(item.sync_id, ...)`` e lo identifica via
    fingerprint. Ritorna ``(raggiunto, identità grezza)``: ``(False, None)``
    se il file o il provider di identificazione non hanno risposto — mai
    un'eccezione che interrompe l'intero scan per un singolo file guasto;
    ``(True, None)`` se il file è stato letto ma non riconosciuto.
    La copia vive in una cartella di lavoro rimossa a fine identificazione."""
    with open_workspace(config.workspace_dir, sync.name) as workspace:
        try:
            name = sync.fetch_partial(
                item.sync_id, str(workspace), sync_session, max_bytes=_FINGERPRINT_MAX_BYTES,
            )
            if name is None:
                return False, None
            return True, identification.identify(workspace.file(name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fingerprint fallito per sync_id=%s ('%s'): %s", item.sync_id, item.path, exc)
            return False, None


# How strong a member's claim on a Musizeer download is (see
# ``db.duplicate_store.resolve_download_for_item``).
_CLAIM_STRENGTH = {"sync_id": 2, "match": 1}


def _settle_download_claims(resolved: list[tuple]) -> list[tuple]:
    """``(item, identity, download, method)`` for every member, where a
    download claimed by several members stays only with the strongest claim
    (an exact sync id link over a tag fallback) and with nobody when the
    strongest claims are equally strong."""
    strongest: dict[str, list[int]] = {}
    for index, (_item, _identity, download, method) in enumerate(resolved):
        if download is None:
            continue
        claims = strongest.setdefault(download.id, [])
        if not claims or _CLAIM_STRENGTH[method] > _CLAIM_STRENGTH[resolved[claims[0]][3]]:
            strongest[download.id] = [index]
        elif _CLAIM_STRENGTH[method] == _CLAIM_STRENGTH[resolved[claims[0]][3]]:
            claims.append(index)
    owners = {claims[0] for claims in strongest.values() if len(claims) == 1}
    return [
        (item, identity, download if index in owners else None, method)
        for index, (item, identity, download, method) in enumerate(resolved)
    ]
