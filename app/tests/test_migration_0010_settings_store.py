"""
Fase 4a: verifica della migrazione 0010 (tabella `settings` unificata) su un
DB *temporaneo* creato ad hoc — mai sul DB reale.

Regola d'oro rispettata: provider finti con nomi generici
(``fakeprovider1``/``fakeprovider2``), mai un nome di provider reale.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlmodel import Session, select

from db.engine import alembic_config
from db.models import Setting

from tests.database import migration_database, new_engine


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR` è letto a runtime dalla migrazione 0010 (per `config.json`):
    un `monkeypatch.setenv` basta, nessun modulo va ricaricato."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def _seed_pre_0010(data_dir: Path, engine) -> None:
    """Popola il DB (già portato a 0009) con dati equivalenti, in forma
    ridotta e provider-agnostica, a quelli verificati sul DB reale: chiavi in
    config.json, righe provider_config (una con dict vuoto, a esercitare lo
    zero-righe), righe di credenziali NON provider_config (che non devono
    MAI migrare), due utenti (uno con priority personale, uno senza), e la
    riga preesistente 'library_scanned' nella vecchia `settings` (key/value)."""
    (data_dir / "config.json").write_text(json.dumps({"alpha": 1, "beta": "two"}))

    # Raw SQL for the same reason as the users below: the model of today has columns
    # (``external_id``, migration 0037) the schema at 0009 does not.
    with engine.begin() as conn:
        for row_id, provider, credential_type, data in (
            ("c1", "fakeprovider1", "provider_config", {"limit": 5, "url": "http://x"}),
            ("c2", "fakeprovider2", "provider_config", {}),
            # NON deve migrare: è un segreto, non una config operativa.
            ("c3", "fakeprovider1", "oauth2_token", {"access_token": "abc"}),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO provider_credentials (id, provider, credential_type, data, updated_at) "
                    "VALUES (:id, :p, :t, :d, :n)"
                ),
                {"id": row_id, "p": provider, "t": credential_type, "d": json.dumps(data), "n": datetime.now(timezone.utc)},
            )

    # Inseriti via SQL grezzo, non tramite il modello ORM `User`: a questo
    # punto della cronologia (revisione 0009) la tabella `users` ha ancora le
    # due colonne che 0011 (Fase 4b) droppa — ma `db.models.User` di OGGI
    # riflette lo schema POST-0011 e non le espone più come campi. Scrivere
    # SQL diretto sulla tabella (già portata a 0009 da `command.upgrade`
    # sopra) esercita esattamente lo schema che la migrazione 0010 vede
    # davvero, indipendentemente da come il modello Python è cambiato dopo.
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        for user_id, auto_like, priority in (
            ("user-a", True, json.dumps(["p1", "p2"])),
            ("user-b", False, None),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, is_active, created_at, "
                    " auto_like_on_sync, metadata_provider_priority) "
                    "VALUES (:id, :username, 'x', 'user', :active, :created_at, :auto_like, :priority)"
                ),
                {
                    "id": user_id, "username": user_id, "created_at": now, "active": True,
                    "auto_like": auto_like, "priority": priority,
                },
            )
        conn.commit()

    with engine.connect() as conn:
        conn.execute(sa.text("INSERT INTO settings (key, value) VALUES ('library_scanned', 'true')"))
        conn.commit()


def _settings_snapshot(engine) -> set[tuple[str, str, str | None, str]]:
    """{(key, scope, user_id, value)} — comodo per confrontare due stati
    ignorando id/updated_at (che cambiano a ogni run per costruzione)."""
    with Session(engine) as session:
        rows = session.exec(select(Setting)).all()
        return {(r.key, r.scope, r.user_id, r.value) for r in rows}


def _credentials(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT provider, credential_type, data FROM provider_credentials")).all()


@pytest.fixture
def migrated_engine(data_dir):
    engine, cfg = migration_database("0009_pending_likes")
    _seed_pre_0010(data_dir, engine)
    command.upgrade(cfg, "0010_settings_store")
    return engine, cfg


# ---------------------------------------------------------------------------
# La migrazione popola le 4 fonti correttamente, senza toccare i segreti
# ---------------------------------------------------------------------------


def test_upgrade_migrates_every_source_without_touching_secrets(migrated_engine):
    engine, _cfg = migrated_engine
    snapshot = _settings_snapshot(engine)

    # config.json -> due righe system.
    assert ("alpha", "system", None, json.dumps(1)) in snapshot
    assert ("beta", "system", None, json.dumps("two")) in snapshot

    # provider_config -> una riga system per short key, namespaced. Il
    # provider con dict vuoto non produce nessuna riga.
    assert ("provider.fakeprovider1.limit", "system", None, json.dumps(5)) in snapshot
    assert ("provider.fakeprovider1.url", "system", None, json.dumps("http://x")) in snapshot
    assert not any(k.startswith("provider.fakeprovider2.") for k, *_ in snapshot)

    # Utenti: user-a ha priority personale + auto_like; user-b solo auto_like
    # (nessuna riga per una priority mai impostata).
    assert ("metadata_provider_priority", "user", "user-a", json.dumps(["p1", "p2"])) in snapshot
    assert ("auto_like_on_sync", "user", "user-a", json.dumps(True)) in snapshot
    assert ("auto_like_on_sync", "user", "user-b", json.dumps(False)) in snapshot
    assert not any(k == "metadata_provider_priority" and uid == "user-b" for k, _s, uid, _v in snapshot)

    # Hazard #2: la riga preesistente nella vecchia `settings` è preservata.
    assert ("library_scanned", "system", None, "true") in snapshot

    # Conteggio esatto: nessuna riga in più, nessuna persa.
    assert len(snapshot) == 8

    # I segreti (tutto tranne provider_config) restano ESATTAMENTE dove
    # erano, in provider_credentials — mai spostati. Le righe provider_config
    # stesse sono invece CANCELLATE una volta copiate (seconda iterazione di
    # questa fase: nulla le legge più da lì) — ne resta solo il segreto.
    creds = _credentials(engine)
    assert len(creds) == 1
    assert creds[0].credential_type == "oauth2_token"
    assert json.loads(creds[0].data) == {"access_token": "abc"}
    assert not any(c.credential_type == "provider_config" for c in creds)


# ---------------------------------------------------------------------------
# Idempotenza: downgrade + re-upgrade riproduce lo STESSO stato logico
# ---------------------------------------------------------------------------


def test_downgrade_restores_legacy_shape_and_reupgrade_is_idempotent(migrated_engine):
    engine, cfg = migrated_engine
    first_snapshot = _settings_snapshot(engine)

    command.downgrade(cfg, "0009_pending_likes")

    with engine.connect() as conn:
        cols = {column["name"] for column in sa.inspect(conn).get_columns("settings")}
        assert cols == {"key", "value"}
        row = conn.execute(sa.text("SELECT value FROM settings WHERE key = 'library_scanned'")).first()
        assert row is not None and row[0] == "true"
        # config.json e users non sono mai stati toccati da upgrade() —
        # restano intatti indipendentemente dal downgrade.
        users_count = conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar()
        assert users_count == 2

    creds = _credentials(engine)
    # downgrade() ricostruisce provider_config da `settings`: fakeprovider1
    # (2 short key non vuote) torna, fakeprovider2 (dict originale {}) no —
    # l'unica asimmetria nota e innocua (vedi il docstring della migrazione).
    # Il segreto oauth2_token non è mai stato toccato.
    assert len(creds) == 2
    by_provider = {(c.provider, c.credential_type): json.loads(c.data) for c in creds}
    assert by_provider[("fakeprovider1", "provider_config")] == {"limit": 5, "url": "http://x"}
    assert by_provider[("fakeprovider1", "oauth2_token")] == {"access_token": "abc"}
    assert ("fakeprovider2", "provider_config") not in by_provider

    command.upgrade(cfg, "0010_settings_store")
    second_snapshot = _settings_snapshot(engine)

    # Stesso insieme logico (key, scope, user_id, value) — nessun duplicato,
    # nessuna perdita, pur avendo rigenerato id/updated_at da zero, e pur
    # essendo la provider_config di fakeprovider1 stata ricostruita (non la
    # riga originale) fra i due upgrade.
    assert second_snapshot == first_snapshot


def test_fresh_database_migrates_cleanly_with_no_prior_data(data_dir):
    """Nessun config.json, nessuna provider_credentials/users popolata: la
    migrazione non deve fallire e produce una tabella `settings` vuota."""
    engine = new_engine(schema=False, addressable=True)
    config = alembic_config(engine.url)
    command.upgrade(config, "0010_settings_store")

    assert _settings_snapshot(engine) == set()

    # The later migrations run on it as well; the only row they write is the
    # storage the old catalog default stood for (0040).
    command.upgrade(config, "head")

    assert _settings_snapshot(engine) == {("storage_provider", "system", None, '"local"')}
