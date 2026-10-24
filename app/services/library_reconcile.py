"""
Library file reconciliation: the library entries (``LibraryEntry``, and the
downloads that landed on their paths) against the files the storage really
holds (``StorageProvider.list_files``).

A file can disappear from under an entry: a later naming fix, or an external
tool, renames or moves it. The entry then keeps the track "in library" with
full playback while the storage answers 404. For every entry whose file is
not in the listing the reconciliation looks for the file, in this order:

1. ``template``: the path the current naming template gives the track
   (``services.library_paths``);
2. ``template_normalized``: the one listed file whose path matches that path
   once case and punctuation are ignored;
3. ``sync_size``: the file a sync provider indexed for the track (its sync
   id): among the listed files whose name ends with that file's title, the
   one with exactly its size (``StorageProvider.size``).

A file found is the entry's new path (and the path of the downloads that
landed on the old one). A file not found marks the entry missing
(``missing_since``): the track is no longer in the library through it — it
may still be, through a sync provider (``db/library_store.py``). Nothing is
deleted, on the storage or in the database, and a missing entry whose file
comes back is restored.

The storage listing is the evidence: without one (a provider that cannot
list, an error, no audio file) nothing changes, and a listing in which not
one entry's file is found, nor found again, is refused (it does not look
like this library: everything would be marked missing).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core import download_states as ds
from core.identifiers import sync_id_type
from musizeer_sdk.text import normalize
from musizeer_sdk.models import LibraryItem, Track
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord
from db.track_store import load_track
from services.library_paths import library_path
from services.library_scan import is_library_audio
from services.storage import resolve_storage_provider

logger = logging.getLogger(__name__)

TEMPLATE, TEMPLATE_NORMALIZED, SYNC_SIZE = "template", "template_normalized", "sync_size"
RELOCATED_BY = (TEMPLATE, TEMPLATE_NORMALIZED, SYNC_SIZE)

# Statuses of a reconciliation that changed nothing.
NO_STORAGE, CANNOT_LIST, EMPTY_LISTING, LISTING_UNRELATED = (
    "no_storage", "cannot_list", "empty_listing", "listing_unrelated",
)

# Sizes asked to the storage for one entry (the most likely files first),
# and for a whole run: each one may be a round trip to a remote server.
SIZE_CHECKS_PER_ENTRY = 6
DEFAULT_MAX_SIZE_CHECKS = 3000

# How many examples of each outcome a report keeps.
SAMPLE_SIZE = 25


@dataclass
class ReconcileReport:
    status: str = "ok"
    dry_run: bool = True
    storage: str | None = None
    files: int = 0                 # audio files listed on the storage
    entries: int = 0               # library entries of that storage
    present: int = 0               # entries whose file is where they say
    restored: int = 0              # marked missing before, back now
    relocated: dict[str, int] = field(default_factory=lambda: {how: 0 for how in RELOCATED_BY})
    superseded: int = 0            # the file is already another entry of the same track
    missing_new: int = 0           # marked missing by this run
    missing_still: int = 0         # already marked missing, still not found
    missing_kept_in_library: int = 0  # missing, but the track stays in the library (sync, another file)
    left_library: int = 0          # tracks no longer in the library after this run
    contested: int = 0             # a file two entries of different tracks would claim
    size_checks: int = 0
    relocations: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    samples: int = SAMPLE_SIZE

    def summary(self) -> dict:
        """The counts, for a routine run (no samples, no paths)."""
        return {
            "status": self.status, "dry_run": self.dry_run, "storage": self.storage,
            "files": self.files, "entries": self.entries, "present": self.present,
            "restored": self.restored, "relocated": sum(self.relocated.values()),
            **{f"relocated_{how}": count for how, count in self.relocated.items()},
            "superseded": self.superseded, "missing_new": self.missing_new,
            "missing_still": self.missing_still, "missing_kept_in_library": self.missing_kept_in_library,
            "left_library": self.left_library, "contested": self.contested,
            "duplicates": len(self.duplicates), "size_checks": self.size_checks,
        }


def _key(text: str) -> str:
    """``text`` with case, accents and punctuation ignored; ``_`` too, the
    character the naming template puts for one a file name cannot hold."""
    return normalize(text.replace("_", " "))


def _path_key(path: str) -> str:
    """A path as :func:`_key` per component, the extension kept apart
    (``a.ogg`` and ``a.flac`` are two files)."""
    pure = PurePosixPath(path)
    return "/".join(_key(part) for part in pure.with_suffix("").parts) + pure.suffix.lower()


class _Listing:
    """The audio files of the storage, indexed for the three lookups."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths: set[str] = set(paths)
        self.by_key: dict[str, list[str]] = defaultdict(list)
        self._names: list[tuple[str, str, str, str]] = []  # (path, suffix, stem key, parent key)
        for path in sorted(self.paths):
            self.by_key[_path_key(path)].append(path)
            pure = PurePosixPath(path)
            self._names.append((path, pure.suffix.lower(), _key(pure.stem), _key(str(pure.parent))))

    def titled(self, item: LibraryItem) -> list[str]:
        """The files whose name ends with the title of ``item`` (same
        extension when known), those in a folder named after its album or
        artist first."""
        title = _key(item.title or "")
        if not title:
            return []
        suffix = f".{item.suffix.lower()}" if item.suffix else None
        album, artist = _key(item.album or ""), _key(item.artist or "")
        found = [
            (path, parent)
            for path, ext, stem, parent in self._names
            if (suffix is None or ext == suffix) and (stem == title or stem.endswith(" " + title))
        ]

        def rank(entry: tuple[str, str]) -> int:
            parent = entry[1]
            return 0 if album and album in parent else 1 if artist and artist in parent else 2

        return [path for path, _parent in sorted(found, key=rank)]


@dataclass
class _Plan:
    entry: LibraryEntry
    action: str               # "relocate" | "supersede" | "missing" | "restore"
    new_path: str | None = None
    how: str | None = None


def reconcile_library_files(
    session: Session,
    registry,
    config,
    auth_manager,
    *,
    apply: bool,
    max_size_checks: int = DEFAULT_MAX_SIZE_CHECKS,
    samples: int = SAMPLE_SIZE,
    now: datetime | None = None,
) -> ReconcileReport:
    """Reconciles the library entries of the configured storage provider with
    its listing. With ``apply`` False (dry run) the database is not touched:
    the report says what a real run would do, with up to ``samples``
    examples of each outcome."""
    report = ReconcileReport(dry_run=not apply, samples=samples)
    storage, storage_name = resolve_storage_provider(registry, config.storage_provider)
    if storage is None:
        report.status = NO_STORAGE
        return report
    report.storage = storage_name

    auth = auth_manager.authenticate(storage)
    # Signing in may have renewed a token: no write stays open over the
    # listing (minutes on a remote storage), the API writes meanwhile.
    session.commit()
    listed = storage.list_files("", auth)
    if listed is None:
        report.status = CANNOT_LIST
        return report
    listing = _Listing(path for path in listed if is_library_audio(path))
    report.files = len(listing.paths)
    if not listing.paths:
        report.status = EMPTY_LISTING
        return report

    entries = session.exec(select(LibraryEntry).where(LibraryEntry.storage_provider == storage_name)).all()
    report.entries = len(entries)
    present = [entry for entry in entries if entry.file_path in listing.paths]
    absent = [entry for entry in entries if entry.file_path not in listing.paths]
    report.present = len(present)

    plans = [_Plan(entry, "restore") for entry in present if entry.missing_since is not None]
    plans += _plan_absent(session, registry, config, auth_manager, storage, auth, absent, entries, listing,
                          report, max_size_checks)
    if absent and not present and all(plan.action == "missing" for plan in plans):
        report.status = LISTING_UNRELATED
        logger.warning(
            "Library reconciliation: none of the %d entries is among the %d files of '%s': nothing changed.",
            len(entries), report.files, storage_name,
        )
        return report
    _count(plans, report)
    _report_library_effects(session, registry, plans, present, report)

    if apply:
        _apply(session, plans, now or datetime.now(timezone.utc), report)
    logger.info("Library reconciliation (%s): %s", "applied" if apply else "dry run", report.summary())
    return report


def _plan_absent(
    session, registry, config, auth_manager, storage, auth, absent, entries, listing, report, max_size_checks,
) -> list[_Plan]:
    by_path = {entry.file_path: entry for entry in entries}
    sync_items = _sync_items(session, registry, auth_manager, [e.track_id for e in absent if e.track_id])
    budget = [max_size_checks]

    found: list[_Plan] = []
    for entry in absent:
        track = _track(session, entry.track_id)
        new_path, how = (None, None)
        if track is not None:
            new_path, how = _locate(entry, track, config, listing, by_path, sync_items.get(entry.track_id, ()),
                                    storage, auth, budget, report)
        if new_path is None:
            found.append(_Plan(entry, "missing"))
            continue
        owner = by_path.get(new_path)
        found.append(_Plan(entry, "supersede" if owner is not None else "relocate", new_path, how))

    # A file two entries would move to: only when they are the same track
    # does one of them take it (the others are superseded by it).
    claims: dict[str, list[_Plan]] = defaultdict(list)
    for plan in found:
        if plan.action == "relocate":
            claims[plan.new_path].append(plan)
    for path, claimants in claims.items():
        if len(claimants) == 1:
            continue
        if len({plan.entry.track_id for plan in claimants}) == 1:
            for plan in claimants[1:]:
                plan.action = "supersede"
        else:
            report.contested += len(claimants)
            for plan in claimants:
                plan.action, plan.new_path, plan.how = "missing", None, None
    return found


def _track(session: Session, track_id: str | None) -> Track | None:
    record = session.get(TrackRecord, track_id) if track_id else None
    return load_track(session, record) if record is not None else None


def _locate(entry, track, config, listing, by_path, items, storage, auth, budget, report):
    """The file of ``entry`` on the storage, and how it was found."""

    def free(path: str) -> bool:
        owner = by_path.get(path)
        return owner is None or owner.track_id == entry.track_id

    suffix = PurePosixPath(entry.file_path).suffix
    expected = library_path(track, config.track_format, suffix)
    if expected in listing.paths and free(expected):
        return expected, TEMPLATE
    near = [path for path in listing.by_key.get(_path_key(expected), ()) if free(path)]
    if len(near) == 1:
        return near[0], TEMPLATE_NORMALIZED

    for item in items:
        if item.size <= 0:
            continue
        same_size = []
        for path in [path for path in listing.titled(item) if free(path)][:SIZE_CHECKS_PER_ENTRY]:
            if budget[0] <= 0:
                return None, None
            budget[0] -= 1
            report.size_checks += 1
            try:
                size = storage.size(path, auth)
            except Exception as exc:  # noqa: BLE001 — a size not known is no evidence
                logger.debug("Size of %s not known: %s", path, exc)
                continue
            if size == item.size:
                same_size.append(path)
        if len(same_size) == 1:
            return same_size[0], SYNC_SIZE
    return None, None


def _sync_items(session, registry, auth_manager, track_ids: list[str]) -> dict[str, list[LibraryItem]]:
    """The files the sync providers indexed for each track (by its sync ids).
    A provider that cannot list its library gives no evidence."""
    if not track_ids:
        return {}
    by_track: dict[str, list[LibraryItem]] = defaultdict(list)
    for sync in registry.all_sync():
        id_type = sync_id_type(sync.name)
        wanted: dict[str, list[str]] = defaultdict(list)
        for chunk in _chunks(track_ids):
            for track_id, value in session.exec(
                select(TrackIdentifierRecord.track_id, TrackIdentifierRecord.value).where(
                    TrackIdentifierRecord.track_id.in_(chunk), TrackIdentifierRecord.id_type == id_type,
                )
            ).all():
                wanted[value].append(track_id)
        if not wanted:
            continue
        try:
            sync_session = auth_manager.authenticate(sync)
            session.commit()
            items = sync.list_library(sync_session)
        except Exception as exc:  # noqa: BLE001 — no evidence from this provider
            logger.warning("Library reconciliation: %s did not list its library: %s", sync.name, exc)
            continue
        for item in items:
            for track_id in wanted.get(item.sync_id, ()):
                by_track[track_id].append(item)
    return by_track


def _chunks(values: list[str], size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _count(plans: list[_Plan], report: ReconcileReport) -> None:
    for plan in plans:
        entry = plan.entry
        if plan.action == "restore":
            report.restored += 1
        elif plan.action == "relocate":
            report.relocated[plan.how] += 1
        elif plan.action == "supersede":
            report.superseded += 1
        elif entry.missing_since is None:
            report.missing_new += 1
        else:
            report.missing_still += 1
        if plan.action in ("relocate", "supersede") and len(report.relocations) < report.samples:
            report.relocations.append({
                "track_id": entry.track_id, "from": entry.file_path, "to": plan.new_path,
                "how": plan.how, "superseded": plan.action == "supersede",
            })


def _report_library_effects(session, registry, plans, present, report) -> None:
    """Which tracks the missing entries take out of the library, and the
    tracks left with more than one file (for whoever decides which to keep)."""
    files_by_track: dict[str, set[str]] = defaultdict(set)
    for entry in present:
        if entry.track_id:
            files_by_track[entry.track_id].add(entry.file_path)
    for plan in plans:
        if plan.action in ("relocate", "supersede") and plan.entry.track_id:
            files_by_track[plan.entry.track_id].add(plan.new_path)

    missing = [plan.entry for plan in plans if plan.action == "missing" and plan.entry.track_id]
    elsewhere = _in_library_without_files(session, registry, [entry.track_id for entry in missing])
    gone: set[str] = set()
    for entry in missing:
        kept = entry.track_id in files_by_track or entry.track_id in elsewhere
        if kept:
            report.missing_kept_in_library += 1
        else:
            gone.add(entry.track_id)
        if len(report.missing) < report.samples:
            report.missing.append({"track_id": entry.track_id, "path": entry.file_path, "in_library": kept})
    report.left_library = len(gone)

    duplicated = {track_id: paths for track_id, paths in files_by_track.items() if len(paths) > 1}
    for track_id in sorted(duplicated):
        record = session.get(TrackRecord, track_id)
        report.duplicates.append({
            "track_id": track_id,
            "artist": record.artist if record else None,
            "title": record.title if record else None,
            "paths": sorted(duplicated[track_id]),
        })


def _in_library_without_files(session, registry, track_ids: list[str]) -> set[str]:
    """The tracks, among ``track_ids``, in the library through something other
    than a stored file: a sync provider's copy, or a download that landed
    without a path (delegated to an external system)."""
    types = [sync_id_type(sync.name) for sync in registry.all_sync()]
    found: set[str] = set()
    for chunk in _chunks(list(set(track_ids))):
        if types:
            found.update(session.exec(select(TrackIdentifierRecord.track_id).where(
                TrackIdentifierRecord.track_id.in_(chunk), TrackIdentifierRecord.id_type.in_(types),
            )).all())
        found.update(session.exec(select(Download.track_id).where(
            Download.track_id.in_(chunk), Download.status == ds.DOWNLOADED, Download.file_path.is_(None),
        )).all())
    return found


def _apply(session: Session, plans: list[_Plan], now: datetime, report: ReconcileReport) -> None:
    for plan in plans:
        entry = plan.entry
        if plan.action == "restore":
            entry.missing_since = None
        elif plan.action == "missing":
            if entry.missing_since is None:
                entry.missing_since = now
        else:
            _move_downloads(session, entry.file_path, plan.new_path)
            if plan.action == "relocate":
                entry.file_path = plan.new_path
                entry.file_format = PurePosixPath(plan.new_path).suffix.lstrip(".").lower()
                entry.missing_since = None
            elif entry.missing_since is None:
                entry.missing_since = now
        session.add(entry)
        try:
            session.commit()
        except IntegrityError:
            # A download registered that path meanwhile: the next run sees it.
            session.rollback()
            report.contested += 1
            logger.warning("Library reconciliation: %s not moved to %s (taken meanwhile)",
                           entry.file_path, plan.new_path)


def _move_downloads(session: Session, old_path: str, new_path: str) -> None:
    for download in session.exec(select(Download).where(Download.file_path == old_path)).all():
        download.file_path = new_path
        session.add(download)
