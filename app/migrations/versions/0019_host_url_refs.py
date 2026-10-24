"""stored host URLs become prefix-free references

Revision ID: 0019_host_url_refs
Revises: 0018_drop_duplicate_members_group_index
Create Date: 2026-09-28

``tracks.track_data`` (``cover_url``/``preview_url``/``stream_url``) e
``user_saved_track_providers.stream_url`` contenevano gli URL delle risorse
servite dall'host così come erano al momento della scrittura: prima senza
prefisso (``/stream/provider/...``), poi con ``/api``. Serviti tali e quali,
i primi caricavano la pagina del frontend invece dell'audio, e i secondi si
romperebbero al primo cambio di ``PUBLIC_API_PREFIX``.

Da qui il DB contiene solo riferimenti senza prefisso
(``hostref:/cover/provider/p/id``, vedi ``core.urls.to_host_ref``), ricostruiti
col prefisso corrente a ogni lettura (``services.public_urls``). Gli URL
assoluti (CDN dei provider) restano come sono; un percorso relativo che non è
una risorsa nota viene rimosso. Idempotente: un riferimento resta tale.

Il downgrade riporta i riferimenti a URL pubblici col prefisso corrente
(``PUBLIC_API_PREFIX``, default ``/api``), la forma che il codice precedente
restituiva senza modificarla.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Sequence, Union

import sqlalchemy as sa
from alembic import op

from core import urls

revision: str = "0019_host_url_refs"
down_revision: Union[str, None] = "0018_drop_duplicate_members_group_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # rewrites stored URLs into hostref: references the previous code cannot read

_BATCH = 500


def _rewrite_track_data(conn, convert: Callable[[str | None], str | None]) -> None:
    rows = conn.execute(sa.text(
        "SELECT internal_id, track_data FROM tracks WHERE track_data IS NOT NULL"
    )).fetchall()
    updates = []
    for internal_id, raw in rows:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        for field in urls.TRACK_URL_FIELDS:
            value = data.get(field)
            if isinstance(value, str):
                new = convert(value)
                if new != value:
                    data[field] = new
                    changed = True
        if changed:
            updates.append({"id": internal_id, "data": json.dumps(data)})
    for start in range(0, len(updates), _BATCH):
        conn.execute(
            sa.text("UPDATE tracks SET track_data = :data WHERE internal_id = :id"),
            updates[start:start + _BATCH],
        )


def _rewrite_saved_stream_urls(conn, convert: Callable[[str | None], str | None]) -> None:
    rows = conn.execute(sa.text(
        "SELECT id, stream_url FROM user_saved_track_providers WHERE stream_url IS NOT NULL"
    )).fetchall()
    updates = [
        {"id": row_id, "url": convert(value)}
        for row_id, value in rows
        if convert(value) != value
    ]
    for start in range(0, len(updates), _BATCH):
        conn.execute(
            sa.text("UPDATE user_saved_track_providers SET stream_url = :url WHERE id = :id"),
            updates[start:start + _BATCH],
        )


def upgrade() -> None:
    conn = op.get_bind()
    _rewrite_track_data(conn, urls.to_host_ref)
    _rewrite_saved_stream_urls(conn, urls.to_host_ref)


def downgrade() -> None:
    prefix = os.environ.get("PUBLIC_API_PREFIX", "/api")
    conn = op.get_bind()
    _rewrite_track_data(conn, lambda value: urls.publish(prefix, value))
    _rewrite_saved_stream_urls(conn, lambda value: urls.publish(prefix, value))
