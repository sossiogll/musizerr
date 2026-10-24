"""introduce the unified `settings` table (Phase 4a: one settings store)

Revision ID: 0010_settings_store
Revises: 0009_pending_likes
Create Date: 2026-09-17

Before this migration a setting's value could live in THREE unrelated
places: ``config.json`` (via ``ConfigManager``), the ``provider_credentials``
table under ``credential_type='provider_config'`` (mixed in with real
secrets), and ad-hoc columns on ``users``. This migration introduces one
table, ``settings``, and moves the non-secret data into it:

  * every key in ``config.json``            -> one ``system`` row per key
  * every ``provider_config`` row            -> one ``system`` row per SHORT
    key, under the namespaced key ``provider.<provider_name>.<short_key>``
    (see ``core/settings_resolver.namespace_provider_key``)
  * each user's ``metadata_provider_priority``, when set -> one ``user`` row
  * each user's ``auto_like_on_sync``         -> one ``user`` row

Secrets do NOT move: ``provider_credentials`` keeps every row whose
``credential_type`` is not ``provider_config`` (OAuth clients/tokens, API
keys, username/password pairs, external-utility account blobs) untouched.

The ``provider_config`` rows themselves ARE deleted from
``provider_credentials`` once copied into ``settings`` (second iteration of
this phase: nothing reads them from there any more — every consumer moved
onto ``SettingsRepository``/``services.provider_context`` — so leaving them
behind would just be silent dead weight, the exact smell this phase exists
to remove). ``downgrade()`` does not depend on those original rows
surviving: it reconstructs one ``provider_config`` row per provider by
regrouping the namespaced ``provider.<name>.<short_key>`` system rows out of
``settings`` back into a short-key JSON blob. One asymmetry is inherent and
harmless: a provider whose ``provider_config`` was an empty dict (``{}`` —
true of a catalog provider on the live DB at the time of writing) leaves no
namespaced row in ``settings`` at all, so downgrade cannot tell "empty dict"
apart from "no row" and does not recreate it. This is not a functional loss:
every reader already treated "row present but empty" and "no row" as the
identical `{}` result.

``config.json`` is left on disk untouched, as a fallback of last resort for
``ConfigManager`` — this migration only stops treating it as the source of
truth, it does not delete it. The two ``users`` columns are also left in
place: dropping them, and moving every production reader/writer onto
``SettingsRepository``, is Phase 4b's business (migration ``0011``).

Hazard #1 (verified against the live DB, not assumed): two DIFFERENT
providers — one metadata, one download — are registered under the SAME
name and therefore share a single ``provider_config`` row today. Their
declared keys happen not to collide, so expanding that one row into "one
settings row per short key, namespaced under the provider NAME" is correct
as-is. Do not try to split them by category — the storage key has always
been the provider name, never the category.

Hazard #2 (found while implementing, not mentioned in the original brief):
a table literally named ``settings`` already existed before this migration
(created in ``0001_baseline``), for one unrelated purpose — the one-shot
``library_scanned`` flag written by ``scripts/library_scan.py``, with the
schema ``(key TEXT PRIMARY KEY, value TEXT)``. This migration repurposes
that same table in place (surrogate ``id``, ``scope``, ``user_id``,
``updated_at``) WITHOUT losing that row: it becomes
``key='library_scanned', scope='system', user_id=NULL``.
``scripts/library_scan.py`` has been updated for the new schema.

Idempotency: re-running this migration's ``upgrade()`` against an
already-migrated database must not duplicate rows. This is achieved by (a)
always reading the row set to preserve from whatever the CURRENT `settings`
table looks like (schema-detected — old 2-column shape or the new
5-column shape, so a second run preserves everything the first run wrote,
including any value changed since via the new settings API) and (b)
de-duplicating on the (key, scope, user_id) triple before inserting. Two of
the three external sources — config.json and the ``users`` columns — are
only ever READ, never mutated, so re-scanning them any number of times is
safe by construction. The third, ``provider_credentials``, IS mutated (its
``provider_config`` rows are deleted once copied — see above), but that
mutation is itself idempotent: a second run finds nothing left to delete
there, and step 1 has already carried forward everything the first run
derived from it into the preserved-rows set, so no data depends on those
original rows surviving for a re-run to be complete.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from core import environment

revision: str = "0010_settings_store"
down_revision: Union[str, None] = "0009_pending_likes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ADR-0007 section 4: what this migration is for the release that ships it (docs/migrations.md).
phase = "contract"  # drops and recreates the settings table with another shape, moving its rows


def _read_existing_settings_rows(conn) -> list[dict]:
    """Righe da preservare dalla tabella `settings` ATTUALE, prima del drop.

    Rileva lo schema per colonne presenti invece di assumerlo: se la
    migrazione girasse una seconda volta (fuori dal normale bookkeeping di
    alembic, che altrimenti la salterebbe del tutto), la tabella si
    troverebbe già nella forma NUOVA (5 colonne) — preservarla generica
    com'è, invece di ri-applicare l'interpretazione "schema vecchio", evita
    di perdere un valore nel frattempo scritto attraverso la nuova API delle
    impostazioni."""
    cols = {column["name"] for column in sa.inspect(conn).get_columns("settings")}
    if "scope" in cols:
        rows = conn.execute(
            sa.text("SELECT id, key, scope, user_id, value, updated_at FROM settings")
        ).fetchall()
        return [
            {"id": r[0], "key": r[1], "scope": r[2], "user_id": r[3], "value": r[4], "updated_at": r[5]}
            for r in rows
        ]
    # Schema originale pre-0010: key PRIMARY KEY, value — un solo uso noto
    # oggi, il flag one-shot 'library_scanned' (vedi scripts/library_scan.py).
    rows = conn.execute(sa.text("SELECT key, value FROM settings")).fetchall()
    return [
        {"id": None, "key": r[0], "scope": "system", "user_id": None, "value": r[1], "updated_at": None}
        for r in rows
    ]


def upgrade() -> None:
    # Import locale (come in 0007_canonicalize_saved_tracks_onto_tracks.py):
    # evita di caricare core/ all'import del modulo di migrazione stesso.
    from core.settings_resolver import namespace_provider_key

    conn = op.get_bind()
    now = datetime.now(timezone.utc)

    preserved_rows = _read_existing_settings_rows(conn)

    op.drop_table("settings")

    op.create_table(
        "settings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="system"),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Vedi la nota su Setting in db/models.py: per scope='system' (user_id
        # sempre NULL) SQLite non basta da solo a impedire un duplicato — è
        # l'upsert query-then-write di SettingsRepository/questa migrazione a
        # garantirlo, non questo vincolo.
        sa.UniqueConstraint("key", "scope", "user_id", name="uq_settings_key_scope_user"),
    )
    op.create_index("ix_settings_key", "settings", ["key"])
    op.create_index("ix_settings_user_id", "settings", ["user_id"])
    op.create_index("ix_settings_key_scope", "settings", ["key", "scope"])

    inserted: set[tuple[str, str, Union[str, None]]] = set()

    def _insert(key: str, scope: str, user_id, value_json: str, updated_at=None, row_id=None) -> None:
        ident = (key, scope, user_id)
        if ident in inserted:
            return
        inserted.add(ident)
        conn.execute(
            sa.text(
                "INSERT INTO settings (id, key, scope, user_id, value, updated_at) "
                "VALUES (:id, :key, :scope, :user_id, :value, :updated_at)"
            ),
            {
                "id": row_id or str(uuid.uuid4()),
                "key": key,
                "scope": scope,
                "user_id": user_id,
                "value": value_json,
                "updated_at": updated_at or now,
            },
        )

    # 1. Righe preesistenti in `settings` (oggi solo 'library_scanned') —
    #    preservate as-is, per prime cosi' vincono su qualunque altra fonte.
    for row in preserved_rows:
        _insert(row["key"], row["scope"], row["user_id"], row["value"],
                updated_at=row["updated_at"], row_id=row["id"])

    # 2. config.json -> una riga system per chiave.
    config_path = Path(environment.data_dir()) / "config.json"
    if config_path.exists():
        try:
            file_data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            file_data = {}
        for key, value in file_data.items():
            _insert(key, "system", None, json.dumps(value))

    # 3. provider_credentials (credential_type='provider_config') -> una riga
    #    system per SHORT key, namespaced provider.<nome>.<short_key>.
    #    Hazard #1 (vedi docstring del modulo): un provider_config puo'
    #    essere condiviso da due provider diversi (stesso nome, categorie
    #    diverse) — l'espansione "una riga per short key sotto il NOME" resta
    #    corretta senza distinguere le categorie.
    provider_rows = conn.execute(
        sa.text("SELECT provider, data FROM provider_credentials WHERE credential_type = 'provider_config'")
    ).fetchall()
    for provider_name, data_json in provider_rows:
        try:
            provider_config = json.loads(data_json or "{}")
        except json.JSONDecodeError:
            provider_config = {}
        for short_key, value in provider_config.items():
            _insert(namespace_provider_key(provider_name, short_key), "system", None, json.dumps(value))

    # Ora che ogni provider_config è stato copiato in `settings`, la riga
    # originale in provider_credentials è dead weight — cancellata (non solo
    # "superseded e lasciata li'"): nulla la legge più (vedi il docstring del
    # modulo su come downgrade() la ricostruisce senza dipendere da questo).
    conn.execute(sa.text("DELETE FROM provider_credentials WHERE credential_type = 'provider_config'"))

    # 4. Colonne per-utente su `users` -> una riga user per utente.
    user_rows = conn.execute(
        sa.text("SELECT id, metadata_provider_priority, auto_like_on_sync FROM users")
    ).fetchall()
    for user_id, metadata_priority_json, auto_like_on_sync in user_rows:
        if metadata_priority_json:
            try:
                priority = json.loads(metadata_priority_json)
            except json.JSONDecodeError:
                priority = None
            if priority is not None:
                _insert("metadata_provider_priority", "user", user_id, json.dumps(priority))
        # auto_like_on_sync e' dichiarata come SettingDef (scope='user') dalla
        # Fase 4b (migrazione 0011), che sposta anche i lettori/scrittori di
        # produzione su SettingsRepository e infine droppa questa colonna —
        # copiata qui per completezza dei dati fin da questa migrazione.
        _insert("auto_like_on_sync", "user", user_id, json.dumps(bool(auto_like_on_sync)))


def downgrade() -> None:
    """Ripristina lo schema originale di `settings` (key PRIMARY KEY, value),
    riportando indietro 'library_scanned' (l'unico dato che viveva
    ESCLUSIVAMENTE in questa tabella prima di questa fase) E i
    ``provider_config`` cancellati da ``provider_credentials`` in upgrade() —
    ricostruiti raggruppando le righe system ``provider.<nome>.<short_key>``
    per nome di provider (vedi il docstring del modulo per l'unica asimmetria
    nota e innocua: un provider il cui config era `{}` non lascia traccia da
    ricostruire). Tutto il resto (config.json, colonne su users) resta
    intatto nella sua fonte originale, mai toccata da upgrade(): non e' una
    perdita di dati, e' la tabella derivata di questa fase che sparisce."""
    from core.settings_resolver import split_provider_key

    conn = op.get_bind()
    now = datetime.now(timezone.utc)

    library_scanned = conn.execute(
        sa.text(
            "SELECT value FROM settings WHERE key = 'library_scanned' "
            "AND scope = 'system' AND user_id IS NULL"
        )
    ).first()

    system_rows = conn.execute(
        sa.text("SELECT key, value FROM settings WHERE scope = 'system'")
    ).fetchall()
    provider_configs: dict[str, dict] = {}
    for key, value_json in system_rows:
        provider_name, short_key = split_provider_key(key)
        if provider_name is None:
            continue
        provider_configs.setdefault(provider_name, {})[short_key] = json.loads(value_json)

    op.drop_index("ix_settings_key_scope", table_name="settings")
    op.drop_index("ix_settings_user_id", table_name="settings")
    op.drop_index("ix_settings_key", table_name="settings")
    op.drop_table("settings")

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    if library_scanned is not None:
        conn.execute(
            sa.text("INSERT INTO settings (key, value) VALUES ('library_scanned', :value)"),
            {"value": library_scanned[0]},
        )

    # Difensivo: se per qualunque motivo provider_credentials avesse GIÀ
    # righe provider_config (non dovrebbe: upgrade() le cancella), ripartire
    # da zero prima di ricostruire evita duplicati — stesso principio
    # "ricostruisci sempre da zero" usato da upgrade() per `settings`.
    conn.execute(sa.text("DELETE FROM provider_credentials WHERE credential_type = 'provider_config'"))

    for provider_name, config in provider_configs.items():
        conn.execute(
            sa.text(
                "INSERT INTO provider_credentials (id, user_id, provider, credential_type, data, updated_at) "
                "VALUES (:id, NULL, :provider, 'provider_config', :data, :updated_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "provider": provider_name,
                "data": json.dumps(config),
                "updated_at": now,
            },
        )
