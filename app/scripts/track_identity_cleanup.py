"""
Pulizia dei dati d'identità delle tracce lasciati dai bug di fix/track-identity.
Idempotente: una seconda esecuzione non trova più nulla da fare.

    docker exec musizerr-app-1 python scripts/track_identity_cleanup.py           # dry-run (default)
    docker exec musizerr-app-1 python scripts/track_identity_cleanup.py --apply   # applica

Funziona su SQLite e su PostgreSQL: TUTTO il lavoro è UNA transazione
(``db.engine.atomic_session``), qualunque commit facciano i passi.

DRY-RUN (default): la transazione viene annullata alla fine; si stampa il
report (prima/dopo) e il database non cambia, nessun task viene accodato. Le
chiamate ai provider (elenco della libreria del sync provider, dettaglio
delle tracce) sono in sola lettura. Su SQLite la transazione tiene il lock di
scrittura finché dura: meglio a worker fermi.

--apply: la transazione viene confermata solo se tutti i passi arrivano in
fondo (un errore annulla tutto). Prima di applicare fai un backup del
database (vedi docs/postgresql.md). Lo script non accoda mai task: i nuovi
preferiti trovati dal like matching lasciano un ``PendingLike`` e la routine
``reconcile_pending_likes`` propaga il like al sync provider al suo prossimo
giro.

Passi, nell'ordine:

1. bitrate dei membri dei duplicati salvati in kbps (``bitRate`` del protocollo Subsonic):
   convertiti in bit/s;
2. gruppi di duplicati: i "risolti" senza nulla di eliminato e con più di un
   keeper (chiave per path virtuale) si riaprono, o si chiudono come
   superati se c'è già un gruppo attivo per la stessa registrazione; le
   proposte dei gruppi attivi si ricalcolano con la chiave per sync id;
3. record uniti per errore (più ISRC, o più id dello stesso provider, di
   versioni o durate incompatibili): ogni identificatore incompatibile col
   record esce in un record suo, e i like importati con quell'id lo seguono;
4. sync id stantii (il sync provider rinnova gli id quando riscansiona):
   sostituiti con l'id attuale dello stesso file, o tolti se il file non
   c'è più;
5. record gemelli (lo stesso valore come id del provider di sync e come
   sync id, su due record): uniti, se compatibili;
6. ogni id del provider di sync su un record porta anche il sync id (lo
   stesso spazio d'identità, ``services.track_identity``);
7. like matching sui like toccati e su quelli ancora da collegare.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from core.audio_quality import QualityScore  # noqa: E402
from core.duplicates import DuplicateCandidate, decide_keeper  # noqa: E402
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import ISRC, is_sync_id_type
from core.matching import base_title_key, duration_tolerance_ms, match_track, version_identity  # noqa: E402
from core.merge import enrich  # noqa: E402
from musizerr_sdk.models import Track
from db.models import (  # noqa: E402
    Download,
    DuplicateGroup,
    DuplicateMember,
    LibraryEntry,
    PendingLike,
    TrackIdentifierRecord,
    TrackRecord,
    UserFavorite,
    UserSavedTrack,
    UserSavedTrackProvider,
)
from db.engine import atomic_session  # noqa: E402
from db.library_store import library_clause  # noqa: E402
from db.track_store import find_or_create_track, stored_track_data, track_from_record  # noqa: E402
from services import favorites  # noqa: E402
from services.auth import auth_manager_for  # noqa: E402
from services.config_manager import get_config  # noqa: E402
from services.credentials import shared_store  # noqa: E402
from services.errors import classify_error  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402
from services.sync_index import sync_item_track  # noqa: E402
from services.track_identity import with_sync_aliases  # noqa: E402

# A lossy bitrate below this is kbps, not bit/s (no audio file is 10 kbit/s).
_KBPS_CEILING = 10_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identifiers(session: Session) -> dict[str, list[TrackIdentifierRecord]]:
    by_track: dict[str, list[TrackIdentifierRecord]] = defaultdict(list)
    for row in session.exec(select(TrackIdentifierRecord)).all():
        by_track[row.track_id].append(row)
    return by_track


def _compatible(a: Track, b: Track) -> bool:
    """Two tracks can be one recording: the same versions in their titles
    and no duration beyond the tolerance. The base title is not asked (the
    identifiers were joined by a shared ISRC, and two catalogs spell a title
    differently: "Seven Days Walking / Day 2: Birdsong" and "Birdsong - Day
    2"), nor album and artist (the same recording is on many releases)."""
    if version_identity(a.title or "") != version_identity(b.title or ""):
        return False
    if a.duration_ms and b.duration_ms and abs(a.duration_ms - b.duration_ms) > duration_tolerance_ms(a.duration_ms, b.duration_ms):
        return False
    return True


def _bare(track: Track) -> Track:
    """The track without identifiers, to compare metadata only."""
    return Track(title=track.title, artist=track.artist, album=track.album, duration_ms=track.duration_ms,
                 year=track.year, artists=track.artists)


def _provider_of(id_type: str) -> str | None:
    return id_type.removesuffix("_id") if id_type.endswith("_id") and not is_sync_id_type(id_type) else None


def metrics(session: Session, sync_names: set[str]) -> dict:
    """The numbers of the analysis of 29 Sep, on the current data."""
    by_track = _identifiers(session)
    per_type = Counter()
    for rows in by_track.values():
        for id_type, count in Counter(r.id_type for r in rows).items():
            if count > 1:
                per_type[id_type] += 1
    twins = 0
    for name in sync_names:
        provider_values = {(r.value, r.track_id) for rows in by_track.values() for r in rows if r.id_type == f"{name}_id"}
        sync_values = {(r.value, r.track_id) for rows in by_track.values() for r in rows if r.id_type == sync_id_type(name)}
        sync_by_value = defaultdict(set)
        for value, track_id in sync_values:
            sync_by_value[value].add(track_id)
        twins += sum(1 for value, track_id in provider_values if sync_by_value.get(value, set()) - {track_id})
    unlinked_sync_likes = 0
    if sync_names:
        rows = session.exec(
            select(UserSavedTrack, UserSavedTrackProvider)
            .join(UserSavedTrackProvider, UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
            .where(UserSavedTrackProvider.provider.in_(sync_names), UserSavedTrack.library_track_id.is_(None))
        ).all()
        unlinked_sync_likes = len({saved.id for saved, _ in rows})
    groups = Counter(g.status for g in session.exec(select(DuplicateGroup)).all())
    in_library = len(set(session.exec(library_clause([sync_id_type(n) for n in sync_names])).scalars().all()))
    kbps_members = len(session.exec(
        select(DuplicateMember).where(DuplicateMember.bitrate > 0, DuplicateMember.bitrate < _KBPS_CEILING)
    ).all())
    return {
        "records_in_library": in_library,
        "records": len(session.exec(select(TrackRecord.internal_id)).all()),
        "records_with_several_values_per_type": dict(sorted(per_type.items())),
        "twin_values_provider_id_vs_sync_id": twins,
        "unlinked_likes_from_sync_provider": unlinked_sync_likes,
        "duplicate_groups_by_status": dict(groups),
        "duplicate_members_with_kbps_bitrate": kbps_members,
    }


# ---------------------------------------------------------------------------
# 1. Bitrates
# ---------------------------------------------------------------------------


def fix_bitrates(session: Session) -> int:
    rows = session.exec(
        select(DuplicateMember).where(DuplicateMember.bitrate > 0, DuplicateMember.bitrate < _KBPS_CEILING)
    ).all()
    for row in rows:
        row.bitrate *= 1000
        session.add(row)
    session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 2. Duplicate groups
# ---------------------------------------------------------------------------


def _decide(members: list[DuplicateMember], prefer_lossless: bool):
    candidates = [
        DuplicateCandidate(
            key=m.sync_id or m.id,
            quality=QualityScore(lossless=bool(m.lossless), bitrate=m.bitrate or 0, sample_rate=m.sample_rate or 0,
                                 codec=m.suffix) if m.quality_known else None,
            deletable=m.origin == "musizerr" and bool(m.download_id),
        )
        for m in members if m.deleted_at is None
    ]
    return decide_keeper(candidates, prefer_lossless=prefer_lossless)


def _recompute(session: Session, group: DuplicateGroup, prefer_lossless: bool) -> None:
    members = session.exec(select(DuplicateMember).where(DuplicateMember.group_id == group.id)).all()
    decision = _decide(members, prefer_lossless)
    group.status, group.reason = decision.status, decision.reason
    for m in members:
        m.is_keeper = decision.keeper_key is not None and (m.sync_id or m.id) == decision.keeper_key
        session.add(m)
    session.add(group)


def fix_duplicate_groups(session: Session, prefer_lossless: bool) -> dict:
    report = {"reopened": [], "superseded": [], "recomputed": 0, "status_changes": Counter()}
    groups = session.exec(select(DuplicateGroup)).all()
    active_by_recording = defaultdict(list)
    for g in groups:
        if g.status in ("proposed", "needs_review"):
            active_by_recording[g.recording_id].append(g)
    for g in groups:
        if g.status != "resolved" or json.loads(g.deleted_paths or "[]"):
            continue
        members = session.exec(select(DuplicateMember).where(DuplicateMember.group_id == g.id)).all()
        if sum(1 for m in members if m.is_keeper) <= 1:
            continue  # a real "handled" with one keeper: the admin's decision
        if active_by_recording.get(g.recording_id):
            g.status, g.reason = "dismissed", "superseded_by_active_group"
            session.add(g)
            report["superseded"].append(g.id)
        else:
            g.resolved_at = None
            _recompute(session, g, prefer_lossless)
            active_by_recording[g.recording_id].append(g)
            report["reopened"].append(g.id)
    for group_list in active_by_recording.values():
        for g in group_list:
            before = g.status
            _recompute(session, g, prefer_lossless)
            report["recomputed"] += 1
            if g.status != before:
                report["status_changes"][f"{before}->{g.status}"] += 1
    session.commit()
    report["status_changes"] = dict(report["status_changes"])
    return report


# ---------------------------------------------------------------------------
# 3. Wrongly merged records
# ---------------------------------------------------------------------------


class Lookup:
    """What an identifier names, asked to its provider (read only, cached).

    A provider that keeps answering "rate limited" (the live app shares its
    quota) is benched for the rest of the run: its identifiers are left as
    they are and counted as not verified, never changed blindly."""

    _RATE_LIMIT_RETRIES = 2

    def __init__(self, registry, auth, live_items: dict[str, object], sync_names: set[str], priority: list[str]):
        self.registry, self.auth, self.live, self.sync_names = registry, auth, live_items, sync_names
        self.isrc_providers = sorted(
            (p for p in registry.all_metadata() if p.supports_isrc_lookup()),
            key=lambda p: priority.index(p.name) if p.name in priority else len(priority),
        )
        self.cache: dict[tuple[str, str], Track | None] = {}
        self.errors = 0
        self.error_kinds: Counter = Counter()
        self.benched: set[str] = set()
        self.not_verified: Counter = Counter()

    def track(self, id_type: str, value: str) -> Track | None:
        key = (id_type, value)
        if key not in self.cache:
            self.cache[key] = self._fetch(id_type, value)
        return self.cache[key]

    def _fetch(self, id_type: str, value: str) -> Track | None:
        if is_sync_id_type(id_type):
            item = self.live.get(value)
            return sync_item_track(item, id_type.removesuffix("_sync_id")) if item else None
        name = _provider_of(id_type)
        if name in self.sync_names:
            # The sync provider's own ids: its library listing says it all
            # (a stale one is step 4's business).
            return sync_item_track(self.live[value], name) if value in self.live else None
        if id_type == ISRC:
            providers = [p for p in self.isrc_providers if p.name not in self.benched]
            if not providers:
                self.not_verified[id_type] += 1
            for provider in providers:
                found = self._ask(provider, lambda p: p.find_by_isrc(value, self.auth.authenticate(p)), id_type)
                if found is not None:
                    return found
            return None
        provider = self.registry.get_metadata_provider(name) if name else None
        if provider is None:
            return None
        if provider.name in self.benched:
            self.not_verified[id_type] += 1
            return None
        return self._ask(provider, lambda p: p.get_track(value, self.auth.authenticate(p)), id_type)

    def _ask(self, provider, call, id_type: str) -> Track | None:
        for attempt in range(self._RATE_LIMIT_RETRIES + 1):
            try:
                return call(provider)
            except Exception as exc:  # noqa: BLE001 — unknown: the identifier is left alone
                kind = classify_error(exc)
                if kind != "rate_limited":
                    self.errors += 1
                    self.error_kinds[kind] += 1
                    return None
                if attempt < self._RATE_LIMIT_RETRIES:
                    time.sleep(min(30, int(getattr(exc, "retry_after", None) or 5)) + 1)
        self.benched.add(provider.name)
        self.not_verified[id_type] += 1
        return None


def _move_likes(session: Session, source_id: str, target_id: str, provider: str, value: str) -> int:
    """The likes imported with ``(provider, value)`` that sit on ``source_id``
    move to ``target_id``; they are matched again."""
    moved = 0
    rows = session.exec(
        select(UserSavedTrack, UserSavedTrackProvider)
        .join(UserSavedTrackProvider, UserSavedTrackProvider.saved_track_id == UserSavedTrack.id)
        .where(UserSavedTrack.track_id == source_id, UserSavedTrackProvider.provider == provider,
               UserSavedTrackProvider.provider_track_id == value)
    ).all()
    for saved, provider_row in rows:
        others = session.exec(select(UserSavedTrackProvider).where(
            UserSavedTrackProvider.saved_track_id == saved.id, UserSavedTrackProvider.id != provider_row.id)).all()
        target = session.exec(select(UserSavedTrack).where(
            UserSavedTrack.user_id == saved.user_id, UserSavedTrack.track_id == target_id)).first()
        if target is None and not others:
            saved.track_id = target_id
            saved.library_track_id, saved.matched_at = None, None
            session.add(saved)
        else:
            if target is None:
                target = UserSavedTrack(user_id=saved.user_id, track_id=target_id, saved_at=saved.saved_at)
                session.add(target)
                session.flush()
            target.library_track_id, target.matched_at = None, None
            session.add(target)
            if session.exec(select(UserSavedTrackProvider).where(
                    UserSavedTrackProvider.saved_track_id == target.id,
                    UserSavedTrackProvider.provider == provider)).first():
                session.delete(provider_row)
            else:
                provider_row.saved_track_id = target.id
                session.add(provider_row)
            if not others:
                session.delete(saved)
        _drop_match_favorite(session, saved.user_id, source_id)
        moved += 1
    return moved


def _drop_match_favorite(session: Session, user_id: str, track_id: str) -> None:
    """A favorite the like matching made from a like that is no longer on
    this record goes, unless another like of the user still is. Nothing is
    unliked on the sync provider."""
    if session.exec(select(UserSavedTrack).where(UserSavedTrack.user_id == user_id,
                                                 UserSavedTrack.library_track_id == track_id)).first():
        return
    favorite = session.exec(select(UserFavorite).where(UserFavorite.user_id == user_id, UserFavorite.track_id == track_id,
                                                       UserFavorite.source == "match")).first()
    if favorite is not None:
        session.delete(favorite)
        for pending in session.exec(select(PendingLike).where(PendingLike.user_id == user_id,
                                                              PendingLike.track_id == track_id)).all():
            session.delete(pending)


def split_wrong_merges(session: Session, lookup: Lookup, registry) -> dict:
    report = {"records_checked": 0, "identifiers_split": 0, "likes_moved": 0, "examples": []}
    for track_id, rows in _identifiers(session).items():
        counts = Counter(r.id_type for r in rows)
        suspect = {t for t, n in counts.items() if n > 1 and t != "mbid"}
        if not suspect:
            continue
        record = session.get(TrackRecord, track_id)
        if record is None:
            continue
        report["records_checked"] += 1
        own = _bare(track_from_record(record))
        for row in [r for r in rows if r.id_type in suspect]:
            other = lookup.track(row.id_type, row.value)
            if other is None or _compatible(own, other):
                continue
            # Everything that names the other recording leaves this record
            # before it finds (or becomes) its own.
            names = {(i.storage_type(), i.value) for i in other.external_ids} | {(row.id_type, row.value)}
            for stale in session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == track_id)).all():
                if (stale.id_type, stale.value) in names:
                    session.delete(stale)
            session.flush()
            target = find_or_create_track(with_sync_aliases(other, registry), session)
            if target.internal_id == track_id:
                continue  # the provider says it is this recording after all
            provider = _provider_of(row.id_type)
            if provider:
                report["likes_moved"] += _move_likes(session, track_id, target.internal_id, provider, row.value)
            report["identifiers_split"] += 1
            if len(report["examples"]) < 15:
                report["examples"].append(f"{record.title!r} -x- {row.id_type}={row.value} ({other.title!r}, {other.duration_ms} ms)")
        session.commit()
    return report


# ---------------------------------------------------------------------------
# 4. Stale sync ids
# ---------------------------------------------------------------------------


def refresh_stale_sync_ids(session: Session, sync_name: str, live_items: dict[str, object]) -> dict:
    """Stale ids of the sync provider (its sync ids and its provider ids)
    get the current id of the same file (one live file matching the record
    with the core matching). A stale id with no replacement is removed only
    when the record stays in the library anyway (another live id, a download
    or a library entry): otherwise it is kept and counted, never a silent
    exit from the library on a match that failed."""
    report = {"stale": 0, "refreshed": 0, "removed_with_live_sibling": 0, "removed_owned_by_file": 0,
              "kept_no_live_match": 0, "examples": []}
    types = {sync_id_type(sync_name), f"{sync_name}_id"}
    buckets: dict[str, list] = defaultdict(list)
    for item in live_items.values():
        buckets[base_title_key(item.title)].append(item)
    for track_id, rows in _identifiers(session).items():
        stale = [r for r in rows if r.id_type in types and r.value not in live_items]
        if not stale:
            continue
        record = session.get(TrackRecord, track_id)
        own = _bare(track_from_record(record)) if record else None
        live_here = {r.value for r in rows if r.id_type in types and r.value in live_items}
        replacement = None
        if own is not None and not live_here:
            matches = [i for i in buckets.get(base_title_key(own.title or ""), []) if match_track(own, i.to_track()).matched]
            replacement = matches[0].sync_id if len(matches) == 1 else None
        owned_by_file = bool(
            session.exec(select(Download.id).where(Download.track_id == track_id, Download.status == "downloaded")).first()
            or session.exec(select(LibraryEntry.id).where(LibraryEntry.track_id == track_id)).first()
        )
        for row in stale:
            report["stale"] += 1
            if replacement:
                if any(r.id_type == row.id_type and r.value == replacement for r in rows):
                    session.delete(row)
                else:
                    if len(report["examples"]) < 10:
                        report["examples"].append(f"{record.title!r}: {row.id_type} {row.value} -> {replacement}")
                    row.value = replacement
                    session.add(row)
                report["refreshed"] += 1
            elif live_here:
                session.delete(row)
                report["removed_with_live_sibling"] += 1
            elif owned_by_file:
                session.delete(row)
                report["removed_owned_by_file"] += 1
            else:
                report["kept_no_live_match"] += 1
        session.commit()
    return report


# ---------------------------------------------------------------------------
# 5. Twins, 6. aliases
# ---------------------------------------------------------------------------


def _merge_into(session: Session, keep: TrackRecord, gone: TrackRecord) -> None:
    keep_ids = {(r.id_type, r.value) for r in session.exec(select(TrackIdentifierRecord).where(
        TrackIdentifierRecord.track_id == keep.internal_id)).all()}
    for row in session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.track_id == gone.internal_id)).all():
        if (row.id_type, row.value) in keep_ids:
            session.delete(row)
        else:
            row.track_id = keep.internal_id
            session.add(row)
    for model in (Download, LibraryEntry):
        for row in session.exec(select(model).where(model.track_id == gone.internal_id)).all():
            row.track_id = keep.internal_id
            session.add(row)
    for saved in session.exec(select(UserSavedTrack).where(UserSavedTrack.library_track_id == gone.internal_id)).all():
        saved.library_track_id = keep.internal_id
        session.add(saved)
    for saved in session.exec(select(UserSavedTrack).where(UserSavedTrack.track_id == gone.internal_id)).all():
        twin = session.exec(select(UserSavedTrack).where(UserSavedTrack.user_id == saved.user_id,
                                                         UserSavedTrack.track_id == keep.internal_id)).first()
        if twin is None:
            saved.track_id = keep.internal_id
            session.add(saved)
            continue
        twin.saved_at = min(twin.saved_at, saved.saved_at)
        for provider_row in session.exec(select(UserSavedTrackProvider).where(
                UserSavedTrackProvider.saved_track_id == saved.id)).all():
            if session.exec(select(UserSavedTrackProvider).where(UserSavedTrackProvider.saved_track_id == twin.id,
                                                                 UserSavedTrackProvider.provider == provider_row.provider)).first():
                session.delete(provider_row)
            else:
                provider_row.saved_track_id = twin.id
                session.add(provider_row)
        session.add(twin)
        session.delete(saved)
    for model in (UserFavorite, PendingLike):
        for row in session.exec(select(model).where(model.track_id == gone.internal_id)).all():
            if session.exec(select(model).where(model.user_id == row.user_id, model.track_id == keep.internal_id)).first():
                session.delete(row)
            else:
                row.track_id = keep.internal_id
                session.add(row)
    merged = track_from_record(keep)
    enrich(merged, track_from_record(gone))
    keep.track_data = stored_track_data(merged)
    session.add(keep)
    session.flush()
    session.delete(gone)


def merge_twins(session: Session, sync_name: str) -> dict:
    report = {"merged": 0, "incompatible": 0, "examples": []}
    provider_type, sync_type = f"{sync_name}_id", sync_id_type(sync_name)
    while True:
        by_value: dict[str, set[str]] = defaultdict(set)
        for row in session.exec(select(TrackIdentifierRecord).where(
                TrackIdentifierRecord.id_type.in_([provider_type, sync_type]))).all():
            by_value[row.value].add(row.track_id)
        pair = next(((value, ids) for value, ids in sorted(by_value.items()) if len(ids) > 1), None)
        if pair is None:
            break
        value, ids = pair
        records = [session.get(TrackRecord, i) for i in sorted(ids)]
        # The record that holds the file (a download, a library entry, the
        # sync id) stays; among equals the oldest.
        def weight(r):
            owned = bool(session.exec(select(Download.id).where(Download.track_id == r.internal_id)).first()
                         or session.exec(select(LibraryEntry.id).where(LibraryEntry.track_id == r.internal_id)).first())
            synced = bool(session.exec(select(TrackIdentifierRecord.id).where(
                TrackIdentifierRecord.track_id == r.internal_id, TrackIdentifierRecord.id_type == sync_type,
                TrackIdentifierRecord.value == value)).first())
            return (not owned, not synced, r.created_at, r.internal_id)
        records.sort(key=weight)
        keep = records[0]
        for gone in records[1:]:
            if _compatible(_bare(track_from_record(keep)), _bare(track_from_record(gone))):
                if len(report["examples"]) < 10:
                    report["examples"].append(f"{gone.title!r} -> {keep.title!r} ({value})")
                _merge_into(session, keep, gone)
                report["merged"] += 1
            else:
                # Not the same recording: the value names the file of the
                # record kept, never the other one.
                for row in session.exec(select(TrackIdentifierRecord).where(
                        TrackIdentifierRecord.track_id == gone.internal_id,
                        TrackIdentifierRecord.id_type.in_([provider_type, sync_type]),
                        TrackIdentifierRecord.value == value)).all():
                    session.delete(row)
                report["incompatible"] += 1
        session.commit()
    return report


def add_sync_aliases(session: Session, sync_name: str, live_items: dict[str, object]) -> int:
    provider_type, sync_type = f"{sync_name}_id", sync_id_type(sync_name)
    by_track = _identifiers(session)
    added = 0
    for track_id, rows in by_track.items():
        synced = {r.value for r in rows if r.id_type == sync_type}
        for row in rows:
            if row.id_type == provider_type and row.value in live_items and row.value not in synced:
                session.add(TrackIdentifierRecord(track_id=track_id, id_type=sync_type, value=row.value))
                added += 1
    session.commit()
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="keep the changes (default: a dry run, all undone)")
    args = parser.parse_args()

    # Never a task from here (the stack may be down): the new favorites leave a
    # PendingLike, and the reconcile_pending_likes routine propagates them.
    dispatched: list[str] = []
    favorites.dispatch = lambda name, **kwargs: dispatched.append(name)

    config = get_config()
    registry = ProviderRegistry()
    sync_names = {s.name for s in registry.all_sync()}
    report: dict = {"mode": "apply" if args.apply else "dry-run"}
    # One transaction for the whole run, whatever the passes commit: a dry run undoes it all, an
    # error undoes it too, --apply keeps it only if everything went through.
    with atomic_session(commit=args.apply) as session:
        report["before"] = metrics(session, sync_names)
        auth = auth_manager_for(shared_store(session))
        sync = registry.get_sync_provider(config.sync_provider) if config.sync_provider else None
        live_items: dict[str, object] = {}
        if sync is not None:
            live_items = {item.sync_id: item for item in sync.list_library(auth.authenticate(sync)) if item.sync_id}
            session.commit()
        report["sync_library_files"] = len(live_items)

        report["bitrates_fixed"] = fix_bitrates(session)
        report["duplicate_groups"] = fix_duplicate_groups(session, config.dedup_prefer_lossless)
        lookup = Lookup(registry, auth, live_items, sync_names, list(config.metadata_provider_priority or []))
        report["wrong_merges"] = split_wrong_merges(session, lookup, registry)
        report["wrong_merges"]["provider_lookup_errors"] = lookup.errors
        report["wrong_merges"]["provider_lookup_error_kinds"] = dict(lookup.error_kinds)
        report["wrong_merges"]["rate_limited_providers"] = sorted(lookup.benched)
        report["wrong_merges"]["identifiers_not_verified"] = dict(lookup.not_verified)
        if sync is not None and live_items:
            report["stale_sync_ids"] = refresh_stale_sync_ids(session, sync.name, live_items)
            report["twins"] = merge_twins(session, sync.name)
            report["sync_aliases_added"] = add_sync_aliases(session, sync.name, live_items)
        else:
            report["stale_sync_ids"] = "skipped: no sync provider library listed"
        report["like_matching"] = favorites.match_likes(session, registry)
        report["after"] = metrics(session, sync_names)
    report["tasks_left_to_the_pending_likes_routine"] = dict(Counter(dispatched))
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
