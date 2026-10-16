"""
``python -m scripts.rotate_credentials_key``: seals every stored credential again with the current
key, reading with any configured one. Dry run by default; reports counts only.
"""
from __future__ import annotations

import io
import json

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from core import secret_box
from core.auth import AccountTarget
from core.secret_box import SecretBox
from db import engine as db_engine
from db.auth_store import CredentialStore
from db.models import ProviderSource
from db.settings_store import SettingsRepository
from scripts import rotate_credentials_key
from services.config_manager import ConfigManager

from tests.credentials_support import KEY, OTHER_KEY, THIRD_KEY, raw, use_keys

SECRETS = ["canary-api-key", "canary-client-secret", "canary-source-token", "canary-webhook", "canary-provider-secret"]


@pytest.fixture(autouse=True)
def _the_scripts_database(engine, monkeypatch):
    monkeypatch.setattr(db_engine, "engine", engine)


@pytest.fixture
def old_world(engine, monkeypatch):
    """Everything stored while OTHER_KEY was the key."""
    use_keys(monkeypatch, OTHER_KEY)
    with Session(engine) as session:
        store = CredentialStore(session)
        store.save_api_key("quay", "canary-api-key")
        store.save_oauth2_client("orbit", "id", "canary-client-secret")
        source = ProviderSource(name="private", url="https://p.example/c.json")
        source.set_credential("canary-source-token")
        session.add(source)
        session.commit()
        SettingsRepository(session).set_system("delegation_webhook_token", "canary-webhook")
        SettingsRepository(session).set_system("provider.catalog.secret", "canary-provider-secret", secret=True)
        SettingsRepository(session).set_system("track_format", "{artist}")
    return engine


def _stored(engine) -> list[str]:
    return (
        [r[0] for r in raw(engine, "SELECT data FROM provider_credentials ORDER BY id")]
        + [r[0] for r in raw(engine, "SELECT credential FROM provider_sources")]
        + [r[0] for r in raw(engine, "SELECT value FROM settings ORDER BY key")]
    )


def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    code = rotate_credentials_key.main(list(argv), out=out)
    return code, out.getvalue()


def _opens_with_only(engine, key: str) -> bool:
    box = SecretBox(key)
    for text in _stored(engine):
        token = text if secret_box.is_sealed(text) else secret_box.enveloped_token(json.loads(text))
        if token is None:
            continue
        try:
            box.open(token)
        except secret_box.Unreadable:
            return False
    return True


def test_the_default_is_a_dry_run_that_changes_nothing_and_counts(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    before = _stored(old_world)

    code, report = _run()

    assert code == 0
    assert _stored(old_world) == before
    assert "Dry run" in report
    assert "provider_credentials: 0 with the current key, 2 to seal again, 0 unreadable, 0 in clear, 0 skipped" in report
    assert "provider_sources: 0 with the current key, 1 to seal again" in report
    assert "settings: 0 with the current key, 2 to seal again" in report


def test_apply_seals_everything_again_with_the_current_key_alone(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)

    code, report = _run("--apply")

    assert code == 0 and "Applied" in report
    assert "2 sealed again" in report
    use_keys(monkeypatch, KEY)  # the old key is no longer needed
    assert _opens_with_only(old_world, KEY)
    with Session(old_world) as session:
        assert CredentialStore(session).get_api_key("quay") == "canary-api-key"
        assert CredentialStore(session).get_oauth2_client("orbit")["client_secret"] == "canary-client-secret"
        assert session.query(ProviderSource).one().credential == "canary-source-token"
        assert SettingsRepository(session).get_system("delegation_webhook_token") == "canary-webhook"
        assert SettingsRepository(session).get_system("provider.catalog.secret") == "canary-provider-secret"
        assert SettingsRepository(session).get_system("track_format") == "{artist}"


def test_it_can_be_run_again_and_finds_nothing_to_do(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    _run("--apply")
    sealed = _stored(old_world)

    code, report = _run("--apply")

    assert code == 0
    assert _stored(old_world) == sealed
    assert "0 sealed again" in report and "2 with the current key" in report


def test_a_new_write_made_during_the_rotation_window_is_already_with_the_new_key(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    with Session(old_world) as session:
        CredentialStore(session).save_api_key("harbor", "canary-new")

    _code, report = _run()

    assert "provider_credentials: 1 with the current key, 2 to seal again" in report


def test_what_no_configured_key_opens_is_left_as_it_is_and_reported(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    with old_world.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = :d WHERE credential_type = 'api_key'"),
                     {"d": SecretBox(THIRD_KEY).seal("{}")})
    lost = [r[0] for r in raw(old_world, "SELECT data FROM provider_credentials WHERE credential_type = 'api_key'")]

    code, report = _run("--apply")

    assert code == 1
    assert [r[0] for r in raw(old_world, "SELECT data FROM provider_credentials WHERE credential_type = 'api_key'")] == lost
    assert "1 unreadable" in report and "Some values were left as they are" in report
    assert "1 sealed again" in report, "the readable one was still moved"


def test_a_credential_in_clear_is_reported_not_sealed(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    with old_world.begin() as conn:
        conn.execute(sa.text("UPDATE provider_credentials SET data = '{\"api_key\": \"canary-in-clear\"}' WHERE credential_type = 'api_key'"))

    code, report = _run("--apply")

    assert code == 1 and "1 in clear" in report
    assert raw(old_world, "SELECT data FROM provider_credentials WHERE credential_type = 'api_key'") == [('{"api_key": "canary-in-clear"}',)]
    assert "canary-in-clear" not in report


def test_the_report_holds_counts_and_nothing_else(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    stored = _stored(old_world)

    _code, dry = _run()
    _code, applied = _run("--apply")

    for text in (dry, applied):
        for secret in (*SECRETS, KEY, OTHER_KEY, *stored):
            assert secret not in text


@pytest.mark.parametrize("key", [None, "", "nope"])
def test_without_a_usable_key_it_is_not_done(old_world, monkeypatch, key):
    before = _stored(old_world)
    use_keys(monkeypatch, key)

    code, report = _run("--apply")

    assert code == 2 and "Not done" in report and "CREDENTIALS_KEY" in report
    assert _stored(old_world) == before


def test_an_apply_is_one_transaction_so_a_failure_leaves_nothing_half_done(old_world, monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY)
    before = _stored(old_world)
    from services import credentials_rotation

    original = credentials_rotation._rotate_column

    def failing(session, box, name, *args, **kwargs):
        if name == "settings":
            raise RuntimeError("the database went away")
        return original(session, box, name, *args, **kwargs)

    monkeypatch.setattr(credentials_rotation, "_rotate_column", failing)

    with pytest.raises(RuntimeError):
        _run("--apply")

    assert _stored(old_world) == before


def test_a_row_changed_between_the_scan_and_the_update_is_not_overwritten(old_world, monkeypatch):
    """The update is conditional on the text that was read: if somebody wrote the row meanwhile, the
    rotation leaves their value and counts the row as skipped (the next run takes it)."""
    from services.credentials_rotation import rotate

    use_keys(monkeypatch, KEY, OTHER_KEY)
    reseal = SecretBox.reseal
    written: list[str] = []

    with Session(old_world) as session:
        def reseal_after_somebody_wrote(box, token):
            if not written:
                replacement = SecretBox(OTHER_KEY).seal('{"api_key": "canary-written-meanwhile"}')
                session.execute(sa.text("UPDATE provider_credentials SET data = :new WHERE data = :old"), {"new": replacement, "old": token})
                written.append(replacement)
            return reseal(box, token)

        monkeypatch.setattr(SecretBox, "reseal", reseal_after_somebody_wrote)

        reports = {report.table: report for report in rotate(session, apply=True)}

        assert reports["provider_credentials"].skipped == 1 and reports["provider_credentials"].rewritten == 1
        assert written[0] in [row[0] for row in session.execute(sa.text("SELECT data FROM provider_credentials"))]
