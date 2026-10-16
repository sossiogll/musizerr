"""
Fase 4b: verifica della migrazione 0011 (drop di `users.auto_like_on_sync` e
`users.metadata_provider_priority`) su un DB *temporaneo* creato ad hoc — mai
sul DB reale.

Copre in particolare la rete di sicurezza esplicitamente richiesta: un utente
la cui riga `settings` non e' ancora stata scritta al momento in cui 0011
gira (perche' creato/modificato fra 0010 e 0011) non deve perdere il proprio
valore quando la colonna viene droppata.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from sqlmodel import Session, select

from db.models import Setting

from tests.database import migration_database


def _insert_user(conn, user_id: str, *, auto_like: bool, priority) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO users "
            "(id, username, password_hash, role, is_active, created_at, "
            " auto_like_on_sync, metadata_provider_priority) "
            "VALUES (:id, :id, 'x', 'user', :active, :created_at, :auto_like, :priority)"
        ),
        {
            "id": user_id,
            "created_at": datetime.now(timezone.utc),
            "active": True,
            "auto_like": auto_like,
            "priority": priority,
        },
    )


def _settings_snapshot(engine) -> set[tuple[str, str, str | None, str]]:
    with Session(engine) as session:
        rows = session.exec(select(Setting)).all()
        return {(r.key, r.scope, r.user_id, r.value) for r in rows}


@pytest.fixture
def engine_at_0010():
    """DB portato a 0010 con due utenti gia' interamente coperti da 0010
    stessa (come nel test di 0010) — la baseline "niente da recuperare"."""
    engine, cfg = migration_database("0009_pending_likes")
    with engine.connect() as conn:
        _insert_user(conn, "user-a", auto_like=True, priority=json.dumps(["p1", "p2"]))
        _insert_user(conn, "user-b", auto_like=False, priority=None)
        conn.commit()
    command.upgrade(cfg, "0010_settings_store")
    return engine, cfg


# ---------------------------------------------------------------------------
# Caso base: 0010 ha gia' coperto tutto, 0011 droppa senza perdere nulla
# ---------------------------------------------------------------------------


def test_upgrade_drops_columns_without_losing_data_already_in_settings(engine_at_0010):
    engine, cfg = engine_at_0010
    before = _settings_snapshot(engine)

    command.upgrade(cfg, "0011_drop_user_setting_columns")

    with engine.connect() as conn:
        cols = {column["name"] for column in sa.inspect(conn).get_columns("users")}
        assert "auto_like_on_sync" not in cols
        assert "metadata_provider_priority" not in cols

    after = _settings_snapshot(engine)
    # 0010 aveva gia' scritto tutto: 0011 non aggiunge, non toglie, non duplica.
    assert after == before
    assert ("auto_like_on_sync", "user", "user-a", json.dumps(True)) in after
    assert ("auto_like_on_sync", "user", "user-b", json.dumps(False)) in after
    assert ("metadata_provider_priority", "user", "user-a", json.dumps(["p1", "p2"])) in after


# ---------------------------------------------------------------------------
# Rete di sicurezza: utente creato/modificato FRA 0010 e 0011
# ---------------------------------------------------------------------------


def test_upgrade_covers_a_user_created_between_0010_and_0011():
    """Un utente registrato dopo che 0010 e' gia' girata (quindi mai copiato
    da quella migrazione) non deve perdere il proprio auto_like_on_sync/
    metadata_provider_priority quando 0011 droppa le colonne."""
    engine, cfg = migration_database("0010_settings_store")

    with engine.connect() as conn:
        # user-late non esisteva quando 0010 e' girata: nessuna riga settings
        # per lui ancora. user-changed esisteva ma il suo auto_like e' stato
        # flippato a True DOPO 0010 (dal vecchio codice, mentre scriveva
        # ancora sulla colonna) — anche questo va recuperato.
        _insert_user(conn, "user-late", auto_like=True, priority=json.dumps(["late-only"]))
        conn.commit()

    command.upgrade(cfg, "0011_drop_user_setting_columns")

    snapshot = _settings_snapshot(engine)
    assert ("auto_like_on_sync", "user", "user-late", json.dumps(True)) in snapshot
    assert ("metadata_provider_priority", "user", "user-late", json.dumps(["late-only"])) in snapshot


def test_upgrade_never_overwrites_an_existing_settings_row():
    """Se una riga settings esiste gia' (es. scritta attraverso la nuova API
    prima che 0011 giri), 0011 non deve sovrascriverla con la colonna."""
    engine, cfg = migration_database("0010_settings_store")

    with engine.connect() as conn:
        _insert_user(conn, "user-a", auto_like=True, priority=None)
        conn.commit()

    with Session(engine) as session:
        # La riga in settings dice False (es. l'utente l'ha appena disattivato
        # tramite l'API), mentre la colonna dice ancora True.
        session.add(Setting(key="auto_like_on_sync", scope="user", user_id="user-a", value=json.dumps(False)))
        session.commit()

    command.upgrade(cfg, "0011_drop_user_setting_columns")

    with Session(engine) as session:
        row = session.exec(
            select(Setting).where(
                Setting.key == "auto_like_on_sync", Setting.scope == "user", Setting.user_id == "user-a",
            )
        ).one()
    assert json.loads(row.value) is False


# ---------------------------------------------------------------------------
# Downgrade: round-trip esatto, poi re-upgrade idempotente
# ---------------------------------------------------------------------------


def test_downgrade_recreates_columns_and_repopulates_from_settings(engine_at_0010):
    engine, cfg = engine_at_0010
    command.upgrade(cfg, "0011_drop_user_setting_columns")

    command.downgrade(cfg, "0010_settings_store")

    with engine.connect() as conn:
        cols = {column["name"] for column in sa.inspect(conn).get_columns("users")}
        assert "auto_like_on_sync" in cols
        assert "metadata_provider_priority" in cols

        rows = conn.execute(
            sa.text("SELECT id, auto_like_on_sync, metadata_provider_priority FROM users ORDER BY id")
        ).fetchall()

    by_id = {r[0]: (bool(r[1]), r[2]) for r in rows}
    assert by_id["user-a"] == (True, json.dumps(["p1", "p2"]))
    # user-b non ha mai avuto una riga metadata_provider_priority: la colonna
    # torna al suo default (NULL), lo stato corretto di "mai scelto".
    assert by_id["user-b"] == (False, None)


def test_downgrade_then_reupgrade_is_idempotent(engine_at_0010):
    engine, cfg = engine_at_0010
    command.upgrade(cfg, "0011_drop_user_setting_columns")
    first_snapshot = _settings_snapshot(engine)

    command.downgrade(cfg, "0010_settings_store")
    command.upgrade(cfg, "0011_drop_user_setting_columns")

    second_snapshot = _settings_snapshot(engine)
    assert second_snapshot == first_snapshot


