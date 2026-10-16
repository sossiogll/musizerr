"""
Una traccia salvata nel DB (``tracks``) restituita a un client. Unico punto
in cui ``tracks.track_data`` diventa una risposta: i riferimenti persistiti
agli URL dell'host (``hostref:``, vedi ``core.urls``) tornano URL pubblici
col prefisso corrente (``services.public_urls``), mai letti tali e quali.
"""
from __future__ import annotations

from musizerr_sdk.identifiers import Identifier
from core.serialization import KIND_TRACK
from db.models import TrackIdentifierRecord, TrackRecord
from sqlmodel import Session

from db.track_store import identifier_rows_by_track, track_data_dict
from services import public_urls


def public_track_dict(
    track_id: str,
    record: TrackRecord | None,
    id_records: list[TrackIdentifierRecord] = (),
) -> dict:
    """La traccia serializzata (schema ``Track``): ``track_data`` quando c'è
    (tutti i metadati della prima richiesta), altrimenti i campi base del
    record. Gli identificatori sono sempre quelli di ``id_records`` (le righe
    di ``track_identifiers`` del record: ``db.track_store.identifiers_by_track``
    o una query per record): ``track_data`` non ne porta."""
    external_ids = [Identifier.from_dict({"type": r.id_type, "value": r.value}).to_dict() for r in id_records]
    stored = track_data_dict(record) if record else None
    if stored is not None:
        track = public_urls.publish_track_dict(stored)
        track["internal_id"] = track_id
        track["kind"] = KIND_TRACK
        track["external_ids"] = external_ids
        return track

    return {
        "kind": KIND_TRACK,
        "internal_id": track_id,
        "title": record.title if record else "",
        "artist": record.artist if record else "",
        "album": record.album if record else "",
        "duration_ms": record.duration_ms if record else 0,
        "cover_url": None,
        "preview_url": None,
        "stream_url": None,
        "provider": "",
        "provider_id": "",
        "artist_provider_id": "",
        "album_provider_id": "",
        "year": None,
        "providers": [],
        "external_ids": external_ids,
        "albums": [],
        "artists": [],
    }


def public_track_dicts(session: Session, records: list[TrackRecord]) -> list[dict]:
    """``public_track_dict`` of each record, with its identifiers read in one
    query (a page of a list)."""
    rows = identifier_rows_by_track(session, [record.internal_id for record in records])
    return [public_track_dict(record.internal_id, record, rows[record.internal_id]) for record in records]
