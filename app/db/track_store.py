from __future__ import annotations

import json
import re

from sqlmodel import Session, select

from core import urls
from core.identifiers import AMBIGUOUS, identity_rank, resolve_identity
from musizerr_sdk.identifiers import Identifier, is_sync_id_type
from core.matching import base_title_key, match_track
from musizerr_sdk.text import strip_version_suffix
from core.merge import enrich
from musizerr_sdk.models import Track
from core.serialization import deserialize_track, serialize_track

from .locks import xact_lock
from .models import TrackIdentifierRecord, TrackRecord
from .upsert import insert_ignore

# The lock that makes "find the track, else create it" one writer at a time.
IDENTITY_LOCK = "track-identity"


def find_or_create_track(track: Track, session: Session, *, replace_stale_sync: bool = True) -> TrackRecord:
    """Resolve *track* to its canonical TrackRecord.

    This is the single identity function shared by download enqueue and
    library sync: two Track objects are the same logical track if they
    share an external id (ISRC/MBID/provider_id; the best rank decides,
    ``core.identifiers.resolve_identity``), or — failing that — the core
    matching accepts them (``_find_by_fuzzy_match``).

    On match, merges *track*'s data into the existing record via
    core.merge.enrich() (so cover_url/albums/artists/providers accumulate
    across sources instead of being overwritten by whichever sync ran
    last) and persists any new identifiers (``add_identifiers``: the only
    writer of ``track_identifiers``; ``tracks.track_data`` never carries
    identifiers, so what is stored here cannot bring an old one back). On no
    match, creates a new TrackRecord + identifiers.

    Mutates ``track.internal_id`` to point at the resolved record, mirroring
    the previous enqueue_track behavior relied on by Celery task dispatch.

    One writer at a time (``IDENTITY_LOCK``, held until the commit): two
    sources of the same song resolved together find the record the first one
    created instead of both creating one.
    """
    xact_lock(session, IDENTITY_LOCK)
    record = _find_by_identifier(track, session) or _find_by_fuzzy_match(track, session)

    if record is None:
        record = TrackRecord(
            internal_id=track.internal_id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration_ms=track.duration_ms,
            track_data=stored_track_data(track),
        )
        session.add(record)
        session.flush()
        add_identifiers(record.internal_id, track.external_ids, session, replace_stale_sync=replace_stale_sync)
        session.commit()
        return record

    existing_track = track_from_record(record) if record.track_data else track
    if existing_track is not track:
        enrich(existing_track, track)

    record.track_data = stored_track_data(existing_track)
    record.title = existing_track.title or record.title
    record.artist = existing_track.artist or record.artist
    record.album = existing_track.album or record.album
    record.duration_ms = existing_track.duration_ms or record.duration_ms
    session.add(record)
    add_identifiers(record.internal_id, track.external_ids, session, replace_stale_sync=replace_stale_sync)
    session.commit()

    track.internal_id = record.internal_id
    return record


def _find_by_identifier(track: Track, session: Session) -> TrackRecord | None:
    """The record the identifiers of *track* resolve to
    (``core.identifiers.resolve_identity``: universal ids, then provider ids,
    then sync ids). When the best identifiers point to more than one record
    the oldest of them is taken, so every process picks the same one."""
    pairs = {(ext_id.storage_type(), ext_id.value) for ext_id in track.external_ids}
    if not pairs:
        return None
    rows = session.exec(
        select(TrackIdentifierRecord).where(TrackIdentifierRecord.value.in_({value for _, value in pairs}))
    ).all()
    hits = [(row.id_type, row.track_id) for row in rows if (row.id_type, row.value) in pairs]
    resolved = resolve_identity(hits)
    if resolved is None:
        return None
    if resolved != AMBIGUOUS:
        return session.get(TrackRecord, resolved)
    best = min(identity_rank(id_type) for id_type, _ in hits)
    candidates = {track_id for id_type, track_id in hits if identity_rank(id_type) == best}
    return session.exec(
        select(TrackRecord).where(TrackRecord.internal_id.in_(candidates))
        .order_by(TrackRecord.created_at, TrackRecord.internal_id)
    ).first()


def _find_by_fuzzy_match(track: Track, session: Session) -> TrackRecord | None:
    """Fallback for sources that share no identifier with the database (a
    file indexed from its tags, a catalog that omits the ISRC in a list):
    the records with the same base title, confirmed by
    ``core.matching.match_track`` — the same decision as everywhere (title
    and version, one artist in common, album/year/duration that agree).
    Never an exact artist string: "Jovanotti" and "Jovanotti, Ackeejuice
    Rockers" are one artist in common. The oldest record wins."""
    if not track.title or not track.artist:
        return None
    key = base_title_key(track.title)
    word = re.search(r"\w+", strip_version_suffix(track.title))
    if not key or word is None:
        return None
    candidates = session.exec(
        select(TrackRecord)
        .where(TrackRecord.title.ilike(f"%{word.group(0)}%"))
        .order_by(TrackRecord.created_at, TrackRecord.internal_id)
    ).all()
    for candidate in candidates:
        if base_title_key(candidate.title) == key and match_track(track, track_from_record(candidate)).matched:
            return candidate
    return None


def add_identifiers(track_id: str, external_ids: list, session: Session, *, replace_stale_sync: bool = True) -> None:
    """Adds identifiers not already present on track_id: the one writer of
    ``track_identifiers`` for a track that already has a record.

    ``external_ids`` are evidence the caller has just gathered from a live
    source (a provider answer, a library listing), never read back from a
    stored payload (``tracks.track_data`` carries none). With
    ``replace_stale_sync`` a sync id replaces the other sync ids of the same
    sync provider on the record: the sync provider gives a file a new id when
    it rescans, and the old one then names nothing (a stream through it is a
    404). A caller that holds the whole live library listing (the indexing
    job) turns it off and settles stale ids against the listing itself,
    because a record with two live files (a duplicate) must keep both."""
    existing = session.exec(
        select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == track_id)
    ).all()
    stale: list[TrackIdentifierRecord] = []
    if replace_stale_sync:
        new_sync = {(ext_id.storage_type(), ext_id.value) for ext_id in external_ids if is_sync_id_type(ext_id.storage_type())}
        held = {(row.id_type, row.value) for row in existing}
        # Only an id the record does not hold yet can replace one: an id it
        # already holds says nothing about its other files (a duplicate).
        new_sync_types = {id_type for id_type, value in new_sync if (id_type, value) not in held}
        stale = [row for row in existing if row.id_type in new_sync_types and (row.id_type, row.value) not in new_sync]
    for row in stale:
        session.delete(row)
    existing_keys = {(r.id_type, r.value) for r in existing if r not in stale}

    new: list[TrackIdentifierRecord] = []
    for ext_id in external_ids:
        key = (ext_id.storage_type(), ext_id.value)
        if key not in existing_keys:
            new.append(TrackIdentifierRecord(track_id=track_id, id_type=key[0], value=key[1]))
            existing_keys.add(key)
    # A writer that added the same identifier meanwhile wins: it is there once.
    insert_ignore(session, *new)


def identifier_rows_by_track(session: Session, track_ids) -> dict[str, list[TrackIdentifierRecord]]:
    """The ``track_identifiers`` rows of each track (the only place the
    identifiers live), in a stable order."""
    ids = list({track_id for track_id in track_ids if track_id})
    found: dict[str, list[TrackIdentifierRecord]] = {track_id: [] for track_id in ids}
    for start in range(0, len(ids), _IN_CHUNK):
        rows = session.exec(
            select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id.in_(ids[start:start + _IN_CHUNK]))
            .order_by(TrackIdentifierRecord.id_type, TrackIdentifierRecord.value)
        ).all()
        for row in rows:
            found[row.track_id].append(row)
    return found


def identifiers_by_track(session: Session, track_ids) -> dict[str, list[Identifier]]:
    """The identifiers of each track, as domain identifiers."""
    return {
        track_id: [Identifier.from_dict({"type": row.id_type, "value": row.value}) for row in rows]
        for track_id, rows in identifier_rows_by_track(session, track_ids).items()
    }


def load_track(session: Session, record: TrackRecord) -> Track:
    """The domain track of ``record`` with its identifiers."""
    return track_from_record(record, identifiers_by_track(session, [record.internal_id])[record.internal_id])


# SQLite's limit on bound parameters is 999 on older builds.
_IN_CHUNK = 500


def stored_track_data(track: Track) -> str:
    """``tracks.track_data`` per *track*: gli URL serviti dall'host diventano
    riferimenti senza prefisso (``core.urls.to_host_ref``), mai URL pubblici.
    Senza identificatori: vivono solo in ``track_identifiers``, una copia nel
    payload invecchierebbe (un sync id sostituito tornerebbe da una lettura).
    Unico punto di scrittura della colonna."""
    data = urls.track_dict_for_storage(serialize_track(track))
    data.pop("external_ids", None)
    return json.dumps(data)


def track_data_dict(record: TrackRecord) -> dict | None:
    """``tracks.track_data`` decodificato, in forma persistita (gli URL
    dell'host sono riferimenti ``hostref:``): prima di restituirlo a un client
    passa da ``services.public_urls.publish_track_dict``. Mai con
    ``external_ids``: i payload scritti prima che gli identificatori vivessero
    solo in ``track_identifiers`` li portano ancora, e qui si ignorano.
    ``None`` se manca o è corrotto. Unico punto di lettura della colonna."""
    if not record.track_data:
        return None
    try:
        data = json.loads(record.track_data)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    data.pop("external_ids", None)
    return data


def track_from_record(record: TrackRecord, identifiers=()) -> Track:
    """
    Ricostruisce la Track di dominio da un TrackRecord. Usa ``track_data`` quando
    presente (piena fedeltà: anno, artisti multipli), altrimenti i campi base.
    Helper unico riusato ovunque serva una Track dal DB.

    Gli identificatori non stanno in ``track_data`` ma in ``track_identifiers``:
    chi li vuole sulla Track li passa in ``identifiers`` (``load_track`` o
    ``identifiers_by_track`` li leggono). Gli URL restano in forma persistita
    (``hostref:``): la Track è per uso interno; una risposta passa da
    ``services.public_urls``.
    """
    track = _track_from_data(record)
    for identifier in identifiers:
        track.add_id(identifier)
    return track


def _track_from_data(record: TrackRecord) -> Track:
    data = track_data_dict(record)
    if data is not None:
        try:
            return deserialize_track(data)
        except Exception:  # noqa: BLE001 — JSON inatteso: fallback ai campi base
            pass
    return Track(
        internal_id=record.internal_id,
        title=record.title,
        artist=record.artist,
        album=record.album,
        duration_ms=record.duration_ms,
    )
