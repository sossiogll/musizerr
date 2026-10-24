"""
The rules of the database guard (ADR-0007 §4): which schema this code can use, and how a backup taken before
a migration is named and pruned.

Pure rules, no I/O: ``db/schema_state.py`` gathers the facts (the revisions the database is at, the ones the
code has, the ``schema_info`` row), ``db/migration_backup.py`` takes the backup, ``services/database_init.py``
asks here what to do.

Expand, then contract. A migration declares its ``phase``:

- ``expand``: new tables and columns (nullable or with a server default), new indexes, backfills that leave the
  old columns as they were, widening a type, dropping or renaming an index no code names. The previous release
  works on the result, so going back one minor needs no downgrade.
- ``contract``: dropping or renaming a column or a table, tightening a constraint (NOT NULL, unique, a foreign
  key, a narrower type), and any change of the data that the previous code cannot read or that loses data (an
  encryption, a deletion). A MAJOR only; the release notes list it.

The database remembers which app can use it (``schema_info``): the newest contract migration applied and the
oldest version that can run on it. An app whose code does not know a revision of the database starts anyway
when everything it does not know is expand (that is the rollback); it refuses when the database holds a
contract migration it does not know.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from core.versions import Version, parse_version

PHASE_EXPAND = "expand"
PHASE_CONTRACT = "contract"
PHASES = (PHASE_EXPAND, PHASE_CONTRACT)

# The oldest version a database may have been last migrated by for THIS version to migrate it ("Upgrade
# paths", ADR-0007 §4): a MAJOR sets it to the last minor of the previous major, and ``init`` refuses an older
# database naming that version. None: no restriction. Also written to the release's release.json.
MIN_UPGRADE_FROM: Optional[str] = None

# The verdicts on a schema.
CURRENT = "current"      # the database is at the revisions of the code
PENDING = "pending"      # the code has migrations the database has not: init applies them
ROLLBACK = "rollback"    # the database is ahead, but everything this code does not know is expand: it can run
TOO_NEW = "too_new"      # the database holds a contract migration this code does not know

# The words that stand for "no revision" in a backup name.
NO_REVISION = "none"
UNVERSIONED = "unversioned"  # tables, but no alembic_version: a database from before migrations


@dataclass(frozen=True)
class SchemaRecord:
    """The row of ``schema_info``: what the last migration left behind."""

    revision: str                         # the heads the database was at, "+"-joined when there are several
    app_version: str                      # the version that last migrated it ("dev" for a development build)
    min_app_version: Optional[str]        # the oldest version that can run on it; None: no contract migration recorded
    contract_revision: Optional[str]      # the newest contract migration applied
    phase: str                            # "contract" when the last run applied one, else "expand"
    migrated_at: Optional[datetime] = None


@dataclass(frozen=True)
class Verdict:
    status: str
    message: str
    unknown: tuple[str, ...] = ()         # the revisions of the database this code does not know


def _join(revisions: Iterable[str]) -> str:
    return "+".join(sorted(revisions))


def revision_label(revisions: Iterable[str]) -> str:
    return _join(revisions) or NO_REVISION


def judge(
    applied: Iterable[str],
    known: Iterable[str],
    heads: Iterable[str],
    record: Optional[SchemaRecord],
    app_version: str,
    *,
    backups: Sequence[str] = (),
) -> Verdict:
    """What this code can do with a database at ``applied`` revisions. ``known`` is every revision the code
    has, ``heads`` the ones it ends at, ``record`` the ``schema_info`` row (None when there is none),
    ``backups`` the names of the backups on disk (to say which one to restore)."""
    applied, known, heads = frozenset(applied), frozenset(known), frozenset(heads)
    unknown = tuple(sorted(applied - known))
    running = parse_version(app_version)

    if record is not None and record.min_app_version is not None:
        minimum = parse_version(record.min_app_version)
        if minimum is not None and running is not None and running < minimum:
            return Verdict(TOO_NEW, _too_new(
                f"it needs Musizerr {minimum} or later (a contract migration, "
                f"{record.contract_revision or 'recorded in schema_info'})", app_version, applied, known, record,
                backups,
            ), unknown)

    if unknown:
        if record is None:
            return Verdict(TOO_NEW, _too_new(
                "it holds revisions this version does not know and has no schema_info row to say they are safe "
                "to run on (it was migrated by hand, or by a release older than the database guard)",
                app_version, applied, known, record, backups,
            ), unknown)
        if record.contract_revision is not None and record.contract_revision not in known:
            return Verdict(TOO_NEW, _too_new(
                f"it was migrated with the contract migration {record.contract_revision}, which this version does "
                "not have and cannot run on",
                app_version, applied, known, record, backups,
            ), unknown)
        return Verdict(ROLLBACK, (
            f"The database is ahead of this version (Musizerr {app_version}): it is at {revision_label(applied)}, "
            f"migrated by {record.app_version}, and this version ends at {revision_label(heads)}. Everything it "
            f"does not know ({', '.join(unknown)}) is expand-only, so this version runs on it as it is; "
            "nothing is migrated."
        ), unknown)

    if applied == heads:
        return Verdict(CURRENT, f"The database is up to date ({revision_label(applied)}).")
    return Verdict(PENDING, (
        f"The database is behind this version (Musizerr {app_version}): it is at {revision_label(applied)} and "
        f"this version needs {revision_label(heads)}. The init step (python -m scripts.init_db, the `init` "
        "service) migrates it, taking a backup first: it has not run, or it failed (see its log)."
    ))


def _too_new(reason: str, app_version: str, applied: frozenset[str], known: frozenset[str],
             record: Optional[SchemaRecord], backups: Sequence[str]) -> str:
    migrated_by = f" by Musizerr {record.app_version}" if record is not None else ""
    restore = restorable_backup(backups, known)
    minimum = record.min_app_version if record is not None else None
    advice = (
        f"Run Musizerr {minimum} or later, " if minimum else "Run the version that migrated it, or a later one, "
    )
    if restore is not None:
        advice += (
            f"or go back to this version with the backup taken before that migration: {restore} (in the backups "
            "folder; python -m scripts.restore_backup, docs/migrations.md)."
        )
    else:
        advice += "or restore the backup taken before that migration (docs/migrations.md); none was found here."
    return (
        f"This version (Musizerr {app_version}) cannot run on this database: it is at {revision_label(applied)}, "
        f"migrated{migrated_by}, and {reason}. {advice}"
    )


def upgrade_path_problem(record: Optional[SchemaRecord], app_version: str,
                         minimum: Optional[str] = None) -> Optional[str]:
    """Why this version must not migrate the database, or None. ``minimum`` is the oldest version a database may
    have been last migrated by (``MIN_UPGRADE_FROM`` when not given): an older one has to go through that version
    first. A database with no ``schema_info`` row was last migrated by a release without the guard, which is
    older than any version a MAJOR can name."""
    minimum = MIN_UPGRADE_FROM if minimum is None else minimum
    wanted = parse_version(minimum)
    if wanted is None:
        return None
    last: Optional[Version] = parse_version(record.app_version) if record is not None else None
    if record is not None and last is None:
        return None  # last migrated by a development build: no version to compare
    if last is not None and last >= wanted:
        return None
    migrated = f"Musizerr {last}" if last is not None else "a release older than the database guard"
    return (
        f"This version (Musizerr {app_version}) upgrades databases last migrated by Musizerr {wanted} or later, and "
        f"this one was last migrated by {migrated}. Start Musizerr {wanted} first (set MUSIZERR_VERSION={wanted}, "
        "let init finish), then this version."
    )


def min_app_version_after(previous: Optional[str], ran_contract: bool, app_version: str) -> Optional[str]:
    """The ``min_app_version`` a migration run leaves: raised to the MAJOR of the version that applied a contract
    migration (a contract migration is a MAJOR's), never lowered. A development build has no version, so it
    cannot raise it; ``contract_revision`` still holds the line for it."""
    if not ran_contract:
        return previous
    running = parse_version(app_version)
    if running is None:
        return previous
    raised = Version(running.major, 0, 0)
    held = parse_version(previous)
    return str(raised if held is None or raised > held else held)


# ---------------------------------------------------------------------------
# The backups taken before a migration
# ---------------------------------------------------------------------------

BACKUP_PREFIX = "premigration"
BACKUP_EXTENSIONS = ("dump", "sqlite3")  # pg_dump custom format, SQLite file
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SAFE = re.compile(r"[^A-Za-z0-9_.+]")
_BACKUP_NAME = re.compile(
    rf"^{BACKUP_PREFIX}-(?P<stamp>\d{{8}}T\d{{6}}Z)-from-(?P<source>[A-Za-z0-9_.+]+)-to-(?P<target>[A-Za-z0-9_.+]+)"
    rf"\.(?P<extension>{'|'.join(BACKUP_EXTENSIONS)})$"
)


@dataclass(frozen=True)
class BackupName:
    name: str
    stamp: str
    source: str       # the revisions the database was at ("none" for an empty one, "unversioned")
    target: str       # the revisions the migration was going to
    extension: str


def backup_name(now: datetime, source: str, target: str, extension: str) -> str:
    """``premigration-20261024T101500Z-from-0042_open_mode_accounts-to-0043_schema_info.dump``: the time first,
    so names sort in the order they were taken."""
    return (
        f"{BACKUP_PREFIX}-{now.strftime(STAMP_FORMAT)}-from-{_SAFE.sub('_', source)}-to-{_SAFE.sub('_', target)}"
        f".{extension}"
    )


def parse_backup_name(name: str) -> Optional[BackupName]:
    match = _BACKUP_NAME.match(name)
    if match is None:
        return None
    return BackupName(name, match["stamp"], match["source"], match["target"], match["extension"])


def backups_to_prune(names: Iterable[str], keep: int) -> list[str]:
    """The backups to delete so that the last ``keep`` remain (never fewer than one). A file that is not
    one of ours is not in the answer."""
    ours = sorted((b for b in map(parse_backup_name, names) if b is not None), key=lambda b: (b.stamp, b.name))
    return [b.name for b in ours[: max(0, len(ours) - max(1, keep))]]


def restorable_backup(names: Iterable[str], known: Iterable[str]) -> Optional[str]:
    """The newest backup taken from a schema this code has every revision of: the one to restore to run this
    version again."""
    known = frozenset(known)
    usable = [
        b for b in map(parse_backup_name, names)
        if b is not None and b.source not in (NO_REVISION, UNVERSIONED) and set(b.source.split("+")) <= known
    ]
    return max(usable, key=lambda b: (b.stamp, b.name)).name if usable else None
