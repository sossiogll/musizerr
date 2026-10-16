"""
The state of each track in a response, decided here once and only read by the
UI (it never derives one from another field):

- ``download_status``: the FILE state, in the download-state vocabulary.
  ``downloaded`` means "in the library" by any source (``db.library_store``);
  otherwise the state of its download attempts, or ``None``.
- ``in_library``: ``download_status == downloaded``, spelled out.
- ``library_id``: the id of the track in the database, when it has one (the
  heart and the favorite routes use it).
- ``favorite`` / ``favorite_pending``: the viewer's favorite, and whether a
  download of theirs will add it when it arrives (only while the download is
  queued or running: a failed one keeps the intent for its retry but shows
  no pending heart).
- ``playback``: ``{"type": "full"|"preview"|"none", "source":
  "library"|"provider"|None}``. Full from the library streams our file
  (``/stream/{library_id}``); full from a provider needs a metadata provider
  that declares ``streams_full_tracks``. ``stream_url`` is set only when the
  playback is full: a client never has to guess what a URL plays.

Reading is free of side effects: nothing here (nor in any GET that uses it)
writes the database. A track that a sync provider answered for during the
request is not "in the library" because of that answer: the library is what
the database holds, and the sync presence gets there through an explicit job
(``services.sync_index``: the ``sync_library_index`` routine — periodic, and
after a library scan or a likes sync — and the download worker), so two
reads of the same page give the same coverage whatever was viewed in between.
"""
from __future__ import annotations

from sqlmodel import Session, select

from core import download_states as ds
from core.identifiers import AMBIGUOUS, resolve_identity, sync_id_type
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from db.favorite_store import favorite_track_ids
from db.library_store import file_states
from db.models import TrackIdentifierRecord, TrackRecord
from db.pending_like_store import tracks_with_favorite_intent
from services import public_urls
from services.capabilities import Viewer
from services.track_identity import alias_pairs, sync_provider_names

FULL, PREVIEW, NONE = "full", "preview", "none"
SOURCE_LIBRARY, SOURCE_PROVIDER = "library", "provider"


def sync_id_types(registry) -> list[str]:
    """The ``track_identifiers`` types of the sync ids of every sync provider."""
    return [sync_id_type(sync.name) for sync in registry.all_sync()]


def _identifier_pairs(track_dict: dict) -> set[tuple[str, str]]:
    """(id_type, value) pairs the DB may know for a track: its native
    provider_id and every external id (ISRC/MBID/other provider ids)."""
    pairs: set[tuple[str, str]] = set()
    provider = track_dict.get("provider")
    provider_id = track_dict.get("provider_id")
    if provider and provider_id:
        pairs.add((Identifier(type=PROVIDER_ID, value=provider_id, provider=provider).storage_type(), provider_id))
    for eid in track_dict.get("external_ids") or []:
        try:
            identifier = Identifier.from_dict(eid)
        except (KeyError, ValueError):
            continue
        pairs.add((identifier.storage_type(), identifier.value))
    return pairs


def known_track_ids(dicts: list[dict], session: Session, registry=None) -> list[str | None]:
    """The database id of each serialized track, None for the ones the
    database cannot place by any of their identifiers (or places on more
    than one record: ambiguous)."""
    sync_names = sync_provider_names(registry) if registry is not None else set()
    return [None if track_id == AMBIGUOUS else track_id for track_id in _resolve_track_ids(dicts, session, sync_names)]


def _resolve_track_ids(dicts: list[dict], session: Session, sync_names: set[str]) -> list[str | None]:
    """The database id of each track: its own ``internal_id`` when that is a
    stored track, else the record its stored identifiers resolve to
    (``core.identifiers.resolve_identity``: universal ids first, then
    provider ids, then sync ids; ``AMBIGUOUS`` when identifiers of the best
    rank point to different records). One query each."""
    internal_ids = {d.get("internal_id") for d in dicts if d.get("internal_id")}
    stored = set(session.exec(
        select(TrackRecord.internal_id).where(TrackRecord.internal_id.in_(internal_ids))
    ).all()) if internal_ids else set()

    pairs_by_item = [alias_pairs(_identifier_pairs(d), sync_names) for d in dicts]
    values = {value for pairs in pairs_by_item for _, value in pairs}
    by_pair: dict[tuple[str, str], set[str]] = {}
    if values:
        for record in session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.value.in_(values))).all():
            by_pair.setdefault((record.id_type, record.value), set()).add(record.track_id)

    resolved: list[str | None] = []
    for d, pairs in zip(dicts, pairs_by_item):
        if d.get("internal_id") in stored:
            resolved.append(d["internal_id"])
        else:
            resolved.append(resolve_identity(
                (id_type, track_id) for id_type, value in pairs for track_id in by_pair.get((id_type, value), ())
            ))
    return resolved


def _sync_hit(track_dict: dict, types: set[str], sync_names: set[str]) -> bool:
    """Whether the track is on a sync provider: it carries a sync id, or it
    is an item of the sync provider itself (its provider id is the sync id,
    ``services.track_identity``)."""
    return any(id_type in types for id_type, _ in alias_pairs(_identifier_pairs(track_dict), sync_names))


def _full_streaming_providers(registry) -> set[str]:
    """The providers whose stream URL plays a whole track: the metadata
    providers that declare it, and the sync providers (``get_stream_url`` is
    their contract)."""
    return {p.name for p in registry.all_metadata() if p.streams_full_tracks} | sync_provider_names(registry)


def _playback(track_dict: dict, in_library: bool, track_id: str | None, viewer: Viewer, full_streamers: set[str]) -> dict:
    if viewer.stream_full and in_library and track_id:
        track_dict["stream_url"] = public_urls.stored_stream_url(track_id)
        return {"type": FULL, "source": SOURCE_LIBRARY}
    providers = {track_dict.get("provider"), *(track_dict.get("providers") or [])}
    if viewer.stream_full and track_dict.get("stream_url") and providers & full_streamers:
        return {"type": FULL, "source": SOURCE_PROVIDER}
    track_dict["stream_url"] = None
    if track_dict.get("preview_url"):
        return {"type": PREVIEW, "source": SOURCE_PROVIDER}
    return {"type": NONE, "source": None}


def annotate_tracks(dicts: list[dict], session: Session, viewer: Viewer, registry) -> None:
    """Sets the state fields described above on every serialized track."""
    if not dicts:
        return
    types = sync_id_types(registry)
    track_ids = known_track_ids(dicts, session, registry)
    known = [track_id for track_id in track_ids if track_id]
    states = file_states(session, known, types)
    favorites = favorite_track_ids(session, viewer.user_id, known) if viewer.user_id else set()
    intents = tracks_with_favorite_intent(session, viewer.user_id, known) if viewer.user_id else set()
    full_streamers = _full_streaming_providers(registry)

    for d, track_id in zip(dicts, track_ids):
        status = states.get(track_id) if track_id else None
        in_library = status == ds.DOWNLOADED
        d["library_id"] = track_id
        d["download_status"] = status
        d["in_library"] = in_library
        d["favorite"] = track_id in favorites
        d["favorite_pending"] = bool(track_id in intents and status in ds.OPEN)
        d["playback"] = _playback(d, in_library, track_id, viewer, full_streamers)


def file_state_of(dicts: list[dict], session: Session, registry) -> list[str | None]:
    """The file state of each serialized track (as ``download_status``
    above), without touching the dicts or indexing anything: for decisions
    such as "download only what is missing"."""
    types = sync_id_types(registry)
    track_ids = [None if track_id == AMBIGUOUS else track_id
                 for track_id in _resolve_track_ids(dicts, session, sync_provider_names(registry))]
    states = file_states(session, [t for t in track_ids if t], types)
    type_set, sync_names = set(types), sync_provider_names(registry)
    return [
        ds.DOWNLOADED if _sync_hit(d, type_set, sync_names) else (states.get(track_id) if track_id else None)
        for d, track_id in zip(dicts, track_ids)
    ]


def file_state_now(session: Session, track_id: str | None, registry) -> str | None:
    """The file state of one stored track right now (as ``download_status``
    above): what a live download event carries, so the UI never has to
    choose between the event and the list."""
    if not track_id:
        return None
    return file_states(session, [track_id], sync_id_types(registry)).get(track_id)


def collection_summary(tracks: list[dict], total: int | None) -> dict:
    """What an album or a playlist shows over its tracks, from the fields
    ``annotate_tracks`` set: the library coverage ("X of N in library", the
    same count the rows show) and the viewer's favorites among the tracks in
    the library (``addable``: in the library, not favorite yet). The coverage
    is the one every discography and Home tile uses
    (``core.download_states.song_coverage``): each row is a song, and two
    rows resolved to one library record count once."""
    return {
        "library": ds.song_coverage(
            ((t.get("library_id") or f"row:{index}", t["download_status"]) for index, t in enumerate(tracks)),
            total=total if total is not None else len(tracks),
        ),
        "favorites": {
            "favorite": sum(1 for t in tracks if t.get("in_library") and t.get("favorite")),
            "addable": sum(1 for t in tracks if t.get("in_library") and not t.get("favorite")),
        },
    }
