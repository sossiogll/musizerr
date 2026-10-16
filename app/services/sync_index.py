"""
The sync providers' libraries, indexed into the database by explicit jobs.

A track is in the library when a sync provider holds a file of it (a sync id
in ``track_identifiers``, ``db.library_store``). That fact is written HERE,
by jobs that have just asked the sync provider, and nowhere else:

- ``index_library``: lists the whole library of a sync provider and brings
  the database in line — every live file has a record carrying its sync id,
  a sync id the provider no longer lists is dropped when the record stays
  explained (another live file, or a file of ours). Runs as its own routine
  (``sync_library_index``: periodically, and right after a library scan or
  a likes sync), and the like matching follows it;
- ``index_track``: asks the sync providers about ONE track (the download
  worker before it fetches a copy, the likes download job).

A read never indexes (``services.track_state``): coverage depends on what is
indexed, not on what was viewed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from sqlmodel import Session, select

from core.auth_manager import ProviderNotConfigured
from core.identifiers import sync_id_type
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from musizerr_sdk.models import Track
from core.routines import SYNC_LIBRARY_INDEX
from core.utc import utc_iso
from db.models import Download, LibraryEntry, TrackIdentifierRecord, TrackRecord
from db.routine_run_store import last_finished_run
from db.track_store import add_identifiers, find_or_create_track, load_track
from services.enrichment import check_sync_availability
from services.errors import public_error
from services.routines import run_error
from services.track_identity import with_sync_aliases

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 100
# A listing that keeps less than this share of the sync ids the database
# knows is taken for partial (an error that did not raise): nothing is dropped.
_MIN_LISTING_SHARE = 0.5

# Progress phases (generic keys, the UI names them).
PHASE_LISTING = "listing"    # asking the sync provider for its whole library
PHASE_INDEXING = "indexing"  # giving every listed file its record
PHASE_PRUNING = "pruning"    # dropping the sync ids no longer listed

# A provider's report status: ``done``, or one of these.
STATUS_DONE = "done"
STATUS_ERROR = "error"                    # the provider could not answer
STATUS_EMPTY_LISTING = "empty_listing"    # it listed nothing: nothing changed
STATUS_PARTIAL_LISTING = "partial_listing"  # it listed too little: nothing dropped
STATUS_NOT_CONFIGURED = "not_configured"  # skipped, not a failure

# The stable error codes of the listing guards (the other failures are
# classified by ``services.errors.public_error``: ``sync_provider_<kind>``).
_GUARD_ERRORS = {
    STATUS_EMPTY_LISTING: ("sync_provider_empty_listing",
                           "The sync provider listed no file: nothing was changed."),
    STATUS_PARTIAL_LISTING: ("sync_provider_partial_listing",
                             "The sync provider listed only part of its library: nothing was removed."),
}
_MAX_ERROR_DETAIL = 300

# The counts of a report summed over the providers in the routine's result.
_TOTALS = ("listed", "created", "attached", "aliased", "stale_dropped", "gone")


@dataclass
class IndexReport:
    sync_provider: str
    listed: int = 0
    already_indexed: int = 0
    aliased: int = 0      # a record had the id as the provider's id: it now carries the sync id too
    attached: int = 0     # a new file (or a new id of a renumbered file) resolved to an existing record
    created: int = 0      # a file no record had
    twins: int = 0        # the id names two records: left for the cleanup (``scripts/track_identity_cleanup.py``)
    stale_dropped: int = 0
    stale_kept: int = 0   # stale, and the record is explained by nothing else
    gone: int = 0         # sync ids the provider no longer lists (dropped or kept)
    pruned: bool = True
    status: str = STATUS_DONE

    def as_dict(self) -> dict:
        return asdict(self)


def sync_item_track(item, sync_name: str) -> Track:
    """The track of a file listed by the sync provider: its metadata, and the
    provider's own id (the sync id, one identity space with it)."""
    track = item.to_track()
    track.provider = sync_name
    track.add_id(Identifier(type=PROVIDER_ID, value=item.sync_id, provider=sync_name))
    return track


def index_libraries(session: Session, registry, auth_manager, *, progress=None) -> list[dict]:
    """``index_library`` for every sync provider; one that fails does not stop
    the others (the next run tries again). Its report says how it failed:
    ``status`` ``error`` with the provider's error code, a listing guard
    (``empty_listing`` / ``partial_listing``) with its own code, or
    ``not_configured`` (skipped). ``progress`` (``services.routines.RunProgress``)
    hears where the indexing is."""
    reports = []
    for sync in registry.all_sync():
        try:
            report = index_library(session, sync, registry, auth_manager, progress=progress).as_dict()
        except ProviderNotConfigured:
            session.rollback()
            reports.append({"sync_provider": sync.name, "status": STATUS_NOT_CONFIGURED})
            continue
        except Exception as exc:  # noqa: BLE001 — the provider could not answer: nothing was changed
            session.rollback()
            logger.warning("Indexing the library of %s failed: %s", sync.name, exc)
            body = public_error(exc, "sync_provider", show_detail=True)
            body["error_detail"] = (body["error_detail"] or "")[:_MAX_ERROR_DETAIL]
            reports.append({"sync_provider": sync.name, "status": STATUS_ERROR, **body})
            continue
        if report["status"] in _GUARD_ERRORS:
            code, message = _GUARD_ERRORS[report["status"]]
            report.update(error_code=code, error_message=message)
        reports.append(report)
    return reports


def index_result(reports: list[dict]) -> dict:
    """The result of the ``sync_library_index`` routine: one report per sync
    provider, the totals, and — when a provider failed or a listing guard
    held — ``status: "error"`` with the first failure's code, so the run is
    recorded as failed (``workers.scheduler.run_routine``)."""
    result: dict = {name: sum(int(r.get(name) or 0) for r in reports) for name in _TOTALS}
    result["providers"] = reports
    failed = [r for r in reports if r.get("error_code")]
    if failed:
        first = failed[0]
        result.update(status=STATUS_ERROR, error_code=first["error_code"], error_message=first.get("error_message"),
                      error_detail=first.get("error_detail"), failed=[r["sync_provider"] for r in failed])
    else:
        result["status"] = "ok"
    return result


def last_index(session: Session) -> dict:
    """The outcome of the last finished ``sync_library_index`` run, for the
    like matching to say what it matched against: ``status`` ``ok`` /
    ``error`` (with the error code) / ``never``, and when it finished."""
    run = last_finished_run(session, SYNC_LIBRARY_INDEX)
    if run is None:
        return {"status": "never"}
    state = {"status": run.status, "finished_at": utc_iso(run.finished_at)}
    if run.status == STATUS_ERROR:
        error = run_error(run) or public_error(run.detail or "", "routine", show_detail=False)
        state.update(error_code=error.get("error_code"), error_message=error.get("error_message"))
    return state


def index_library(session: Session, sync, registry, auth_manager, *, progress=None) -> IndexReport:
    report = IndexReport(sync_provider=sync.name)
    _report(progress, PHASE_LISTING, provider=sync.name, force=True)
    live = {item.sync_id: item for item in sync.list_library(auth_manager.authenticate(sync)) if item.sync_id}
    report.listed = len(live)
    if not live:
        # An empty answer is no evidence that the library is empty.
        report.status, report.pruned = STATUS_EMPTY_LISTING, False
        return report

    sync_type, provider_type = sync_id_type(sync.name), f"{sync.name}_id"
    holders: dict[str, set[str]] = {}
    synced: set[tuple[str, str]] = set()
    for row in session.exec(
        select(TrackIdentifierRecord).where(TrackIdentifierRecord.id_type.in_([sync_type, provider_type]))
    ).all():
        holders.setdefault(row.value, set()).add(row.track_id)
        if row.id_type == sync_type:
            synced.add((row.track_id, row.value))
    existing_records = set(session.exec(select(TrackRecord.internal_id)).all())

    total = len(live)
    _report(progress, PHASE_INDEXING, provider=sync.name, processed=0, total=total)
    for processed, (value, item) in enumerate(live.items(), start=1):
        owners = holders.get(value, set())
        if owners:
            indexed = {track_id for track_id in owners if (track_id, value) in synced}
            if indexed:
                report.already_indexed += 1
                report.twins += int(len(owners) > 1)
            elif len(owners) == 1:
                track_id = next(iter(owners))
                add_identifiers(track_id, [Identifier(type=sync_type, value=value)], session, replace_stale_sync=False)
                synced.add((track_id, value))
                report.aliased += 1
            else:
                report.twins += 1
        else:
            track = with_sync_aliases(sync_item_track(item, sync.name), registry)
            record = find_or_create_track(track, session, replace_stale_sync=False)
            if record.internal_id in existing_records:
                report.attached += 1
            else:
                report.created += 1
                existing_records.add(record.internal_id)
            synced.add((record.internal_id, value))
        if processed % _COMMIT_EVERY == 0:
            # Progress is written in its own transaction: only after a commit.
            session.commit()
            _report(progress, PHASE_INDEXING, provider=sync.name, processed=processed, total=total)
    session.commit()
    _report(progress, PHASE_INDEXING, provider=sync.name, processed=total, total=total)

    _report(progress, PHASE_PRUNING, provider=sync.name, force=True)
    _drop_stale(session, sync_type, set(live), report)
    return report


def _report(progress, phase: str, **fields) -> None:
    if progress is not None:
        progress(phase, **fields)


def _drop_stale(session: Session, sync_type: str, live: set[str], report: IndexReport) -> None:
    """The sync ids the provider no longer lists. A renumbered file already
    resolved to its record above (the new id sits beside the old one), so a
    stale id goes when the record has a live sync id left, or a file of ours.
    A record that nothing else explains keeps it (and is counted): it leaves
    the library only when someone can say the file is gone."""
    rows = session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.id_type == sync_type)).all()
    known = {(row.track_id, row.value) for row in rows}
    report.gone = sum(1 for _, value in known if value not in live)
    if known and sum(1 for _, value in known if value in live) < _MIN_LISTING_SHARE * len(known):
        report.pruned = False
        report.status = STATUS_PARTIAL_LISTING
        logger.warning("Sync library listing of %s looks partial (%d live of %d known): nothing dropped",
                       report.sync_provider, sum(1 for _, v in known if v in live), len(known))
        return

    by_track: dict[str, list[TrackIdentifierRecord]] = {}
    for row in rows:
        by_track.setdefault(row.track_id, []).append(row)
    for track_id, track_rows in by_track.items():
        stale = [row for row in track_rows if row.value not in live]
        if not stale:
            continue
        if any(row.value in live for row in track_rows) or _owned_by_file(session, track_id):
            for row in stale:
                session.delete(row)
            report.stale_dropped += len(stale)
        else:
            report.stale_kept += len(stale)
    session.commit()


def _owned_by_file(session: Session, track_id: str) -> bool:
    return bool(
        session.exec(select(Download.id).where(Download.track_id == track_id, Download.status == "downloaded")).first()
        or session.exec(select(LibraryEntry.id).where(LibraryEntry.track_id == track_id)).first()
    )


def index_track(session: Session, registry, auth_manager, track_id: str) -> bool:
    """Asks every sync provider whether it holds a file of the stored track
    and records the id it answers with. True when one does. A provider that
    cannot answer is skipped (the question is asked again next time)."""
    record = session.get(TrackRecord, track_id)
    if record is None:
        return False
    track = load_track(session, record)
    found = False
    for sync in registry.all_sync():
        try:
            availability = check_sync_availability(sync, track, auth_manager.authenticate(sync))
        except Exception as exc:  # noqa: BLE001 — unknown, not "absent"
            logger.debug("Sync availability of %s on %s not known: %s", track_id, sync.name, exc)
            continue
        if availability:
            add_identifiers(track_id, [Identifier(type=sync_id_type(sync.name), value=availability.sync_id)], session,
                            replace_stale_sync=False)
            found = True
    if found:
        session.commit()
    return found
