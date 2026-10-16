"""tracks.track_data without identifiers: they live only in track_identifiers

Revision ID: 0025_track_data_without_identifiers
Revises: 0024_refresh_token_rotation
Create Date: 2026-10-03

``tracks.track_data`` used to carry a copy of the track's identifiers
(``external_ids``). The copy aged: a cleanup of ``track_identifiers`` left it
as it was, and every list built from it brought the replaced sync ids back
(``add_identifiers`` through the sync-hit indexing of a read), which is how
the twin records returned three days after they were merged.

The identifiers are now read only from ``track_identifiers``; the code
ignores ``external_ids`` in a payload and never writes it
(``db.track_store.stored_track_data``). This migration removes the copy from
the rows already stored. Nothing is lost: the table is the authority, and an
identifier that only the payload had is exactly the kind that was stale.

Idempotent: a payload with no ``external_ids`` (or one that is not a JSON
object) is left as it is. The downgrade rebuilds the copy from the table, so
a previous release finds the payloads it expects.
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_track_data_without_identifiers"
down_revision: Union[str, None] = "0024_refresh_token_rotation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYNC_ID_SUFFIX = "_sync_id"
_UNIVERSAL = {"isrc", "mbid"}


def _payloads(conn):
    return conn.execute(sa.text("SELECT internal_id, track_data FROM tracks WHERE track_data IS NOT NULL")).fetchall()


def _write(conn, internal_id: str, data: dict) -> None:
    conn.execute(
        sa.text("UPDATE tracks SET track_data = :data WHERE internal_id = :id"),
        {"data": json.dumps(data), "id": internal_id},
    )


def upgrade() -> None:
    conn = op.get_bind()
    for internal_id, raw in _payloads(conn):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and "external_ids" in data:
            data.pop("external_ids")
            _write(conn, internal_id, data)


def _identifier_dict(id_type: str, value: str) -> dict:
    """The serialized identifier of a ``track_identifiers`` row (as
    ``musizerr_sdk.identifiers.Identifier.to_dict`` wrote it)."""
    if id_type in _UNIVERSAL or id_type.endswith(_SYNC_ID_SUFFIX) or not id_type.endswith("_id"):
        return {"type": id_type, "value": value}
    return {"type": id_type, "value": value, "provider": id_type.removesuffix("_id")}


def downgrade() -> None:
    conn = op.get_bind()
    by_track: dict[str, list[dict]] = {}
    for track_id, id_type, value in conn.execute(
        sa.text("SELECT track_id, id_type, value FROM track_identifiers ORDER BY id_type, value")
    ):
        by_track.setdefault(track_id, []).append(_identifier_dict(id_type, value))
    for internal_id, raw in _payloads(conn):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            data["external_ids"] = by_track.get(internal_id, [])
            _write(conn, internal_id, data)
