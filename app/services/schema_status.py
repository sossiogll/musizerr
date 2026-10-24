"""
Which schema the database is at, for the admins to see (ADR-0007 §2): ``GET /health`` and ``GET /admin/updates``
(the About page). Facts the migrations left in ``alembic_version`` and ``schema_info``; the judgment on them is the
start check's (``services/database_init.py``).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.engine import Engine

from core.utc import utc_iso
from db import schema_state


def schema_status(db_engine: Engine) -> Optional[dict[str, Any]]:
    """``{revision, migrated_by, min_app_version, contract_revision, phase, migrated_at}``: the revisions the
    database is at now, and what the last migration run recorded (None for the fields of a database that was
    migrated by hand or before ``schema_info``). None when the database cannot be asked or was never migrated:
    a status page must not fail because of it."""
    try:
        applied = schema_state.applied_revisions(db_engine)
        record = schema_state.read_record(db_engine)
    except Exception:  # noqa: BLE001 - a database that does not answer is /health's other fields' business
        return None
    if not applied:
        return None
    return {
        "revision": "+".join(sorted(applied)),
        "migrated_by": record.app_version if record else None,
        "min_app_version": record.min_app_version if record else None,
        "contract_revision": record.contract_revision if record else None,
        "phase": record.phase if record else None,
        "migrated_at": utc_iso(record.migrated_at) if record and record.migrated_at else None,
    }
