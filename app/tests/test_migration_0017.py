"""
Migrazione 0017: le righe della cache dei fingerprint senza identità (che
potevano essere file non raggiunti o sotto una soglia vecchia) spariscono,
quelle con un'identità restano.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import command

from tests.database import migration_database




def test_unidentified_rows_are_forgotten_and_identified_ones_kept():
    engine, cfg = migration_database("0016_drop_anonymous_downloads")
    with engine.begin() as conn:
        for path, recording in (("sid-known", "rec-1"), ("sid-unknown", None)):
            conn.execute(
                sa.text(
                    "INSERT INTO duplicate_fingerprint_cache (id, path, size, recording_id, computed_at) "
                    "VALUES (:id, :path, 1, :recording, :now)"
                ),
                {"id": str(uuid.uuid4()), "path": path, "recording": recording, "now": datetime.now(timezone.utc)},
            )

    command.upgrade(cfg, "0017_retry_unidentified_fingerprints")

    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT path FROM duplicate_fingerprint_cache")).scalars().all()
    assert rows == ["sid-known"]
