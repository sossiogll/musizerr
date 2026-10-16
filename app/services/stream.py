"""
Risoluzione del sync_id di una traccia: cache su DB, poi lookup via
``SyncProvider.find_candidates`` quando la cache non ha nulla; un sync_id
trovato via API viene salvato in cache.
"""
from __future__ import annotations

import logging

from sqlmodel import select

from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import Identifier
from db.models import TrackIdentifierRecord, TrackRecord
from db.track_store import add_identifiers, load_track
from services.enrichment import check_sync_availability

logger = logging.getLogger(__name__)


def resolve_sync_id(session, track_id, sync, auth_manager, store):
    """
    Cerca il sync_id per un track_id. Prima nel DB (cache), poi via check_sync_availability().
    Se trovato via API, lo salva nel DB per evitare ricerche future.
    Ritorna il sync_id o None.
    """
    # Cache: cerca sync_id gia salvato
    cached = session.exec(
        select(TrackIdentifierRecord)
        .where(
            TrackIdentifierRecord.track_id == track_id,
            TrackIdentifierRecord.id_type == sync_id_type(sync.name),
        )
    ).first()
    if cached:
        return cached.value

    # Cerca via API
    track_record = session.get(TrackRecord, track_id)
    if not track_record:
        return None

    try:
        sync_session = auth_manager.authenticate(sync)
        avail = check_sync_availability(sync, load_track(session, track_record), sync_session)
    except Exception as exc:
        logger.warning("Sync availability check failed for track %s: %s", track_id, exc)
        return None

    if not avail:
        return None

    # Cache il risultato (l'unico scrittore degli identificatori: non sostituisce
    # gli altri sync id, li regola l'indicizzazione della libreria)
    add_identifiers(track_id, [Identifier(type=sync_id_type(sync.name), value=avail.sync_id)], session,
                    replace_stale_sync=False)
    session.commit()
    return avail.sync_id
