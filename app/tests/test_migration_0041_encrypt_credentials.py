"""
Migration 0041 seals the credentials the earlier versions stored in clear (docs/credentials.md):
``provider_credentials.data``, ``provider_sources.credential`` and the catalog's secret settings. It
needs the key, refuses without it before it changes a row, can run twice, and its downgrade opens
everything back. Run on SQLite and on PostgreSQL alike (the suite's dialect).
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlmodel import Session

from core import secret_box
from core.secret_box import SecretBox
from db.auth_store import CredentialStore

from tests.credentials_support import KEY, OTHER_KEY, THIRD_KEY, raw, raw_scalar, use_keys
from tests.database import migration_database

BEFORE, HEAD = "0040_storage_provider_explicit", "0041_encrypt_credentials"
NOW = datetime(2026, 10, 10, 12, 0)

TOKEN_DATA = json.dumps({"access_token": "canary-access-1", "refresh_token": "canary-refresh-1"})
CLIENT_DATA = json.dumps({"client_id": "id-1", "client_secret": "canary-client-secret"})
KEY_DATA = json.dumps({"api_key": "canary-api-key"})
UTILITY_DATA = json.dumps({"main": {"cookie": "canary-cookie"}})
SOURCE_TOKEN = "canary-source-token"
WEBHOOK = "canary-webhook"


@pytest.fixture
def db():
    return migration_database(BEFORE)


def _seed(engine, *, extra_settings: dict[str, str] | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at) VALUES ('alice', 'alice', 'x', 'user', :t, :n)"),
            {"t": True, "n": NOW})
        for row_id, user_id, provider, kind, data in (
            ("c1", "alice", "orbit", "oauth2_token", TOKEN_DATA),
            ("c2", None, "orbit", "oauth2_client", CLIENT_DATA),
            ("c3", None, "quay", "api_key", KEY_DATA),
            ("c4", None, "pier", "pier_cookies", UTILITY_DATA),
        ):
            conn.execute(sa.text(
                "INSERT INTO provider_credentials (id, user_id, provider, credential_type, data, updated_at) "
                "VALUES (:id, :u, :p, :k, :d, :n)"), {"id": row_id, "u": user_id, "p": provider, "k": kind, "d": data, "n": NOW})
        for row_id, name, url, token in (("s1", "private", "https://p.example/c.json", SOURCE_TOKEN), ("s2", "open", "https://o.example/c.json", None)):
            conn.execute(sa.text(
                "INSERT INTO provider_sources (id, name, url, enabled, added_at, credential, allow_insecure_registry) "
                "VALUES (:id, :name, :url, :e, :n, :c, :i)"), {"id": row_id, "name": name, "url": url, "e": True, "n": NOW, "c": token, "i": False})
        settings = {
            "delegation_webhook_token": json.dumps(WEBHOOK),
            "track_format": json.dumps("{artist}/{title}"),
            "provider.catalog.secret": json.dumps("canary-provider-secret"),
            **(extra_settings or {}),
        }
        for key, value in settings.items():
            conn.execute(sa.text(
                "INSERT INTO settings (id, key, scope, value, updated_at) VALUES (:id, :key, 'system', :value, :n)"),
                {"id": f"set-{key}", "key": key, "value": value, "n": NOW})


def _credentials(engine) -> dict[str, str]:
    return dict(raw(engine, "SELECT id, data FROM provider_credentials"))


def _sources(engine) -> dict[str, str | None]:
    return dict(raw(engine, "SELECT id, credential FROM provider_sources"))


def _settings(engine) -> dict[str, str]:
    return dict(raw(engine, "SELECT key, value FROM settings"))


def _revision(engine) -> str:
    return raw_scalar(engine, "SELECT version_num FROM alembic_version")


def _everything(engine) -> tuple:
    return _credentials(engine), _sources(engine), _settings(engine)


def test_the_upgrade_seals_every_credential_and_nothing_else(db):
    engine, cfg = db
    _seed(engine)

    command.upgrade(cfg, HEAD)

    box = SecretBox(KEY)
    credentials = _credentials(engine)
    assert {row_id: box.open(text) for row_id, text in credentials.items()} == {
        "c1": TOKEN_DATA, "c2": CLIENT_DATA, "c3": KEY_DATA, "c4": UTILITY_DATA,
    }, "each sealed text opens to exactly what was stored"
    assert all(secret_box.is_sealed(text) for text in credentials.values())

    sources = _sources(engine)
    assert box.open(sources["s1"]) == SOURCE_TOKEN
    assert sources["s2"] is None, "no token stays no token"

    settings = _settings(engine)
    envelope = json.loads(settings["delegation_webhook_token"])
    assert box.open(secret_box.enveloped_token(envelope)) == json.dumps(WEBHOOK)
    assert json.loads(settings["track_format"]) == "{artist}/{title}", "a setting that is not secret is untouched"
    assert json.loads(settings["provider.catalog.secret"]) == "canary-provider-secret", (
        "the secrets providers declare are not known to a migration: the registry seals them (services/secret_settings.py)"
    )

    sealed_texts = [*credentials.values(), sources["s1"], settings["delegation_webhook_token"]]
    for canary in ("canary-access-1", "canary-refresh-1", "canary-client-secret", "canary-api-key", "canary-cookie",
                   SOURCE_TOKEN, WEBHOOK):
        assert not any(canary in text for text in sealed_texts)
    assert _revision(engine) == HEAD


def test_what_the_upgrade_wrote_is_what_the_application_reads(db, monkeypatch):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)

    with Session(engine) as session:
        shared = CredentialStore(session)
        assert shared.get_api_key("quay") == "canary-api-key"
        assert shared.get_oauth2_client("orbit") == {"client_id": "id-1", "client_secret": "canary-client-secret"}
        assert shared.get_external_utility_accounts("pier", "pier_cookies") == {"main": {"cookie": "canary-cookie"}}
        assert CredentialStore(session, user_id="alice").own_oauth2_token("orbit").data["refresh_token"] == "canary-refresh-1"

    from services.config_manager import ConfigManager

    assert ConfigManager(engine=engine).delegation_webhook_token == WEBHOOK


def test_running_it_twice_changes_nothing(db):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)
    first = _everything(engine)

    command.stamp(cfg, BEFORE)  # as if it had not run
    command.upgrade(cfg, HEAD)

    assert _everything(engine) == first, "a value already sealed is not sealed again"


def test_a_database_with_some_values_already_sealed_gets_only_the_rest_sealed(db):
    engine, cfg = db
    _seed(engine)
    already = SecretBox(KEY).seal(KEY_DATA)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = :d WHERE id = 'c3'"), {"d": already})

    command.upgrade(cfg, HEAD)

    credentials = _credentials(engine)
    assert credentials["c3"] == already
    assert all(secret_box.is_sealed(text) for text in credentials.values())


def test_a_value_sealed_with_a_previous_key_is_accepted_as_it_is(db, monkeypatch):
    engine, cfg = db
    _seed(engine)
    old = SecretBox(OTHER_KEY).seal(KEY_DATA)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = :d WHERE id = 'c3'"), {"d": old})
    use_keys(monkeypatch, KEY, OTHER_KEY)

    command.upgrade(cfg, HEAD)

    assert _credentials(engine)["c3"] == old, "the rotation command moves it, not the migration"


@pytest.mark.parametrize("key", [None, "", "  ", "not-a-key", "change-me-in-production"])
def test_without_a_usable_key_it_refuses_before_changing_a_row(db, monkeypatch, key):
    engine, cfg = db
    _seed(engine)
    before = _everything(engine)
    use_keys(monkeypatch, key)

    with pytest.raises(RuntimeError, match="CREDENTIALS_KEY") as refused:
        command.upgrade(cfg, HEAD)

    assert _everything(engine) == before
    assert _revision(engine) == BEFORE
    if key and len(key.strip()) > 8:
        assert key.strip() not in str(refused.value)


def test_a_value_sealed_with_a_key_that_is_not_configured_stops_it_and_nothing_is_changed(db):
    engine, cfg = db
    _seed(engine)
    foreign = SecretBox(THIRD_KEY).seal(KEY_DATA)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = :d WHERE id = 'c3'"), {"d": foreign})
    before = _everything(engine)

    with pytest.raises(RuntimeError, match=r"1 stored credential\(s\) are already encrypted with a key that is not"):
        command.upgrade(cfg, HEAD)

    assert _everything(engine) == before, "the rows still in clear were not sealed either"
    assert _revision(engine) == BEFORE


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def test_the_downgrade_opens_everything_back_to_what_was_stored(db):
    engine, cfg = db
    sealed_by_a_provider = secret_box.envelope(SecretBox(KEY).seal(json.dumps("canary-provider-secret")))
    _seed(engine)
    originals = _everything(engine)
    command.upgrade(cfg, HEAD)
    with engine.begin() as conn:  # what the registry seals after the upgrade
        conn.execute(sa.text("UPDATE settings SET value = :v WHERE key = 'provider.catalog.secret'"), {"v": json.dumps(sealed_by_a_provider)})

    command.downgrade(cfg, BEFORE)

    assert _everything(engine) == originals
    assert _revision(engine) == BEFORE


def test_the_downgrade_after_the_upgrade_after_the_downgrade_is_still_consistent(db):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)
    command.downgrade(cfg, BEFORE)

    command.upgrade(cfg, HEAD)

    assert all(secret_box.is_sealed(text) for text in _credentials(engine).values())


def test_the_downgrade_needs_the_key_and_refuses_without_it(db, monkeypatch):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)
    sealed = _everything(engine)
    use_keys(monkeypatch, None)

    with pytest.raises(RuntimeError, match="CREDENTIALS_KEY"):
        command.downgrade(cfg, BEFORE)

    assert _everything(engine) == sealed and _revision(engine) == HEAD


def test_the_downgrade_refuses_what_it_cannot_open_and_writes_nothing(db, monkeypatch):
    engine, cfg = db
    _seed(engine)
    command.upgrade(cfg, HEAD)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = :d WHERE id = 'c3'"), {"d": SecretBox(THIRD_KEY).seal(KEY_DATA)})
    sealed = _everything(engine)

    with pytest.raises(RuntimeError, match=r"1 stored credential\(s\) cannot be opened"):
        command.downgrade(cfg, BEFORE)

    assert _everything(engine) == sealed and _revision(engine) == HEAD
