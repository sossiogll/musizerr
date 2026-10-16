"""
The settings declared secret (``SettingDef.secret``: a catalog entry, or one a provider declares) are
stored sealed (docs/credentials.md) and read back opened; the ones that are not secret stay readable
JSON. The catalog's are sealed by the migration; the providers' by the write, and, for a value an
older version stored in clear, by ``services/secret_settings.py`` once the registry knows the schema.
"""
from __future__ import annotations

import json
import logging

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from core import secret_box
from core.settings_resolver import namespace_provider_key
from db.models import User
from db.settings_store import SettingsRepository
from services.config_manager import ConfigManager
from services.provider_context import _get_provider_config
from services.secret_settings import seal_declared_secrets
from services.settings import SettingsService

from tests.credentials_support import KEY, THIRD_KEY, raw, raw_scalar, use_keys
from tests.settings_support import registry_of_every_category

TOKEN = "canary-webhook-token-71ab"
PROVIDER_SECRET = "canary-provider-secret-5d20"
SECRET_KEY = namespace_provider_key("catalog", "secret")
URL_KEY = namespace_provider_key("catalog", "url")


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine):
    return ConfigManager(engine=engine)


@pytest.fixture
def admin(session):
    user = User(username="admin", password_hash="x", role="admin")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def service(session, config):
    return SettingsService(session, config, registry_of_every_category())


def _row(engine, key: str) -> str:
    return raw_scalar(engine, "SELECT value FROM settings WHERE key = :key AND scope = 'system'", key=key)


def test_a_catalog_secret_is_stored_sealed_and_read_through_the_config(engine, config):
    config.set("delegation_webhook_token", TOKEN)

    stored = _row(engine, "delegation_webhook_token")
    assert TOKEN not in stored
    assert secret_box.enveloped_token(json.loads(stored)) is not None
    assert config.delegation_webhook_token == TOKEN
    assert ConfigManager(engine=engine).delegation_webhook_token == TOKEN, "another process reads it too"


def test_a_secret_a_provider_declares_is_stored_sealed_when_written_through_the_settings(engine, session, service, admin):
    service.write_values({SECRET_KEY: PROVIDER_SECRET, URL_KEY: "https://catalog.example"}, admin)

    secret_row, plain_row = _row(engine, SECRET_KEY), _row(engine, URL_KEY)
    assert PROVIDER_SECRET not in secret_row and secret_box.enveloped_token(json.loads(secret_row))
    assert json.loads(plain_row) == "https://catalog.example", "a setting that is not secret stays readable JSON"
    assert service.effective_values(admin)[SECRET_KEY] == PROVIDER_SECRET
    assert SettingsRepository(session).get_system(SECRET_KEY) == PROVIDER_SECRET


def test_the_provider_is_given_the_opened_value_in_its_context(session, service, admin):
    service.write_values({SECRET_KEY: PROVIDER_SECRET}, admin)

    assert _get_provider_config("catalog", session)["secret"] == PROVIDER_SECRET
    assert service.provider_config("catalog")["secret"] == PROVIDER_SECRET


def test_a_secret_can_be_changed_and_emptied(engine, service, admin):
    service.write_values({SECRET_KEY: PROVIDER_SECRET}, admin)
    first = _row(engine, SECRET_KEY)

    service.write_values({SECRET_KEY: "canary-second"}, admin)
    assert _row(engine, SECRET_KEY) != first
    assert service.effective_values(admin)[SECRET_KEY] == "canary-second"

    service.write_values({SECRET_KEY: ""}, admin)
    assert secret_box.enveloped_token(json.loads(_row(engine, SECRET_KEY))), "an empty secret is sealed like any other"
    assert service.effective_values(admin)[SECRET_KEY] == ""


def test_a_string_setting_that_looks_sealed_is_still_just_a_string(engine, service, admin):
    looks_sealed = "enc:v1:gAAAAABnotreallyasealedvalue"

    service.write_values({URL_KEY: looks_sealed}, admin)

    assert service.effective_values(admin)[URL_KEY] == looks_sealed


def test_a_sealed_value_read_with_another_key_is_the_default_not_the_ciphertext(engine, session, config, service, admin, monkeypatch, caplog):
    config.set("delegation_webhook_token", TOKEN)
    service.write_values({SECRET_KEY: PROVIDER_SECRET}, admin)
    use_keys(monkeypatch, THIRD_KEY)
    caplog.set_level(logging.INFO)

    fresh = ConfigManager(engine=engine)
    assert fresh.delegation_webhook_token == "", "the declared default, as if it had never been set"
    after = SettingsService(session, fresh, registry_of_every_category())
    declared_default = next(s.default for s in after.schema() if s.key == SECRET_KEY)
    values = after.effective_values(admin)
    assert values[SECRET_KEY] == declared_default
    assert "enc:v1" not in json.dumps(values)
    assert "secret" not in after.provider_config("catalog")
    assert TOKEN not in caplog.text and PROVIDER_SECRET not in caplog.text and "CREDENTIALS_KEY" in caplog.text

    use_keys(monkeypatch, KEY)
    assert ConfigManager(engine=engine).delegation_webhook_token == TOKEN


def test_a_catalog_secret_in_clear_is_not_returned(engine, config, caplog):
    caplog.set_level(logging.WARNING)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO settings (id, key, scope, value, updated_at) "
            "VALUES ('legacy', 'delegation_webhook_token', 'system', '\"canary-in-clear\"', '2026-01-01 00:00:00')"
        )

    assert config.delegation_webhook_token == ""
    assert "canary-in-clear" not in caplog.text


def test_the_secrets_of_a_provider_found_in_clear_are_sealed_once_the_registry_knows_them(engine, session):
    keys = (SECRET_KEY, URL_KEY, "delegation_webhook_token", "track_format")
    with engine.begin() as connection:
        for key, value in zip(keys, (PROVIDER_SECRET, "https://catalog.example", TOKEN, "{artist}/{title}")):
            connection.execute(
                sa.text("INSERT INTO settings (id, key, scope, value, updated_at) VALUES (:id, :key, 'system', :value, '2026-01-01 00:00:00')"),
                {"id": f"id-{key}", "key": key, "value": json.dumps(value)},
            )

    assert seal_declared_secrets(registry_of_every_category(), engine=engine) == 1
    sealed = [key for key in keys if secret_box.enveloped_token(json.loads(_row(engine, key)))]

    assert sealed == [SECRET_KEY], "only the secrets providers declare; the catalog's belong to the migration"
    assert SettingsRepository(session).get_system(SECRET_KEY) == PROVIDER_SECRET
    assert json.loads(_row(engine, "track_format")) == "{artist}/{title}"
    assert seal_declared_secrets(registry_of_every_category(), engine=engine) == 0, "idempotent"
    assert SettingsRepository(session).get_system(SECRET_KEY) == PROVIDER_SECRET


def test_sealing_does_nothing_for_a_provider_that_declares_no_secret(engine):
    from tests.conftest import FakeMetadataProvider
    from services.provider_registry import ProviderRegistry
    from tests.loopback import as_proxy

    registry = ProviderRegistry(discover=False)
    registry.register(as_proxy(FakeMetadataProvider("plain")))

    assert seal_declared_secrets(registry, engine=engine) == 0


def test_sealing_is_not_fatal_when_the_database_does_not_answer():
    from sqlalchemy import create_engine  # noqa: PLC0415

    broken = create_engine("sqlite:///" + "/nonexistent-directory/x.db")

    assert seal_declared_secrets(registry_of_every_category(), engine=broken) == 0
