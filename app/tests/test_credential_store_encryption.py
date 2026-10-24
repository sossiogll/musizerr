"""
Every credential the host stores is stored SEALED (``db/credential_cipher.py``, docs/credentials.md) and
read back opened, whichever kind it is. What is REALLY in the table is asserted with raw SQL under the
store; what a value that cannot be opened does is asserted per kind.
"""
from __future__ import annotations

import json
import logging

import pytest
import sqlalchemy as sa
from sqlmodel import Session

from core.auth import AccountTarget
from core import secret_box
from core.secret_box import SecretBox
from db.auth_store import CredentialStore
from db.models import User
from musizeer_sdk.auth import AccountModel

from tests.credentials_support import KEY, OTHER_KEY, THIRD_KEY, raw, raw_scalar, use_keys

PERSONAL, SHARED = AccountTarget.PERSONAL, AccountTarget.SHARED

# Values nobody else would write: each must never appear in the table, a log or an error.
ACCESS, REFRESH, PASSWORD = "canary-access-8f3a", "canary-refresh-2c71", "canary-password-b9e0"
CLIENT_SECRET, API_KEY, UTILITY_COOKIE = "canary-client-secret-47d1", "canary-api-key-9a65", "canary-cookie-33fe"
CANARIES = [ACCESS, REFRESH, PASSWORD, CLIENT_SECRET, API_KEY, UTILITY_COOKIE]


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def alice(session):
    user = User(username="alice", password_hash="x", role="user")
    session.add(user)
    session.commit()
    return user.id


def _data_column(engine) -> list[str]:
    return [row[0] for row in raw(engine, "SELECT data FROM provider_credentials")]


def _connect_everything(session, alice):
    own = CredentialStore(session, user_id=alice)
    shared = CredentialStore(session)
    own.save_oauth2_token("orbit", {"access_token": ACCESS, "refresh_token": REFRESH}, PERSONAL, external_id="acct-1")
    shared.save_oauth2_token("orbit", {"access_token": ACCESS + "-shared"}, SHARED)
    own.save_credentials("harbor", {"username": "ann", "password": PASSWORD}, PERSONAL)
    shared.save_oauth2_client("orbit", "client-id", CLIENT_SECRET)
    shared.save_api_key("quay", API_KEY)
    shared.save_oauth2_app_token("orbit", {"access_token": ACCESS + "-app"})
    shared.save_external_utility_account("pier", "pier_cookies", "main", {"cookie": UTILITY_COOKIE})


def test_every_kind_of_credential_is_stored_sealed_and_read_back(engine, session, alice):
    _connect_everything(session, alice)

    stored = _data_column(engine)
    assert len(stored) == 7
    assert all(secret_box.is_sealed(text) for text in stored), "nothing is in clear"
    assert not any(canary in text for text in stored for canary in CANARIES)

    own, shared = CredentialStore(session, user_id=alice), CredentialStore(session)
    assert own.own_oauth2_token("orbit").data == {"access_token": ACCESS, "refresh_token": REFRESH}
    assert shared.shared_oauth2_token("orbit").data == {"access_token": ACCESS + "-shared"}
    assert own.own_credentials("harbor").data == {"username": "ann", "password": PASSWORD}
    assert shared.get_oauth2_client("orbit") == {"client_id": "client-id", "client_secret": CLIENT_SECRET}
    assert shared.get_api_key("quay") == API_KEY
    assert shared.get_oauth2_app_token("orbit") == {"access_token": ACCESS + "-app"}
    assert shared.get_external_utility_accounts("pier", "pier_cookies") == {"main": {"cookie": UTILITY_COOKIE}}


def test_the_identifiers_and_the_routing_columns_stay_readable(engine, session, alice):
    """Only the secret is sealed: what the host looks rows up by (provider, type, user, the account's id) is not."""
    _connect_everything(session, alice)

    rows = raw(engine, "SELECT provider, credential_type, external_id FROM provider_credentials ORDER BY provider, credential_type")

    assert ("orbit", "oauth2_token", "acct-1") in rows
    assert CredentialStore(session, user_id=alice).own_external_id("orbit") == "acct-1"
    assert CredentialStore(session).linked_user_id("orbit", "acct-1") == alice


def test_the_same_value_written_twice_is_sealed_differently(engine, session):
    store = CredentialStore(session)
    store.save_api_key("quay", API_KEY)
    first = raw_scalar(engine, "SELECT data FROM provider_credentials")

    store.save_api_key("quay", API_KEY)

    assert raw_scalar(engine, "SELECT data FROM provider_credentials") != first
    assert store.get_api_key("quay") == API_KEY


def test_a_refreshed_token_is_stored_sealed_and_replaces_only_the_row_it_came_from(engine, session, alice):
    store = CredentialStore(session, user_id=alice)
    store.save_oauth2_token("orbit", {"access_token": ACCESS, "refresh_token": REFRESH}, PERSONAL)
    current = store.own_oauth2_token("orbit")
    assert secret_box.is_sealed(current.raw), "what a compare-and-swap checks is the sealed text"

    assert store.replace_oauth2_token("orbit", current, {"access_token": "canary-new", "refresh_token": REFRESH})

    (stored,) = _data_column(engine)
    assert secret_box.is_sealed(stored) and "canary-new" not in stored
    assert store.own_oauth2_token("orbit").data["access_token"] == "canary-new"
    # The same `current` again: the row is no longer what was read, the second refresh is dropped.
    assert not store.replace_oauth2_token("orbit", current, {"access_token": "canary-late"})
    assert store.own_oauth2_token("orbit").data["access_token"] == "canary-new"


def test_a_value_sealed_with_a_previous_key_is_read_and_a_new_write_uses_the_primary(engine, session, monkeypatch):
    use_keys(monkeypatch, OTHER_KEY)
    CredentialStore(session).save_api_key("quay", API_KEY)

    use_keys(monkeypatch, KEY, OTHER_KEY)
    store = CredentialStore(session)
    assert store.get_api_key("quay") == API_KEY
    store.save_api_key("quay", API_KEY + "-2")

    (stored,) = _data_column(engine)
    assert SecretBox(KEY).open(stored) == json.dumps({"api_key": API_KEY + "-2"}), "sealed with the primary alone"


# ---------------------------------------------------------------------------
# A value that cannot be opened: for every reader the credential is not there
# ---------------------------------------------------------------------------


@pytest.fixture
def lost_key(engine, session, alice, monkeypatch):
    """Everything connected, then the key is replaced by another one (and the old one is gone)."""
    _connect_everything(session, alice)
    use_keys(monkeypatch, THIRD_KEY)
    return alice


def test_with_the_wrong_key_no_account_is_connected_and_no_value_is_returned(session, lost_key, caplog):
    caplog.set_level(logging.INFO)
    own, shared = CredentialStore(session, user_id=lost_key), CredentialStore(session)

    assert own.own_oauth2_token("orbit") is None
    assert shared.shared_oauth2_token("orbit") is None
    assert own.own_credentials("harbor") is None
    assert shared.get_oauth2_client("orbit") is None and shared.get_oauth2_client_id("orbit") is None
    assert shared.get_api_key("quay") is None
    assert shared.get_oauth2_app_token("orbit") is None
    assert shared.get_external_utility_accounts("pier", "pier_cookies") == {}

    assert [c.data for c in own.oauth2_token_candidates("orbit", AccountModel.PERSONAL_WITH_SHARED)] == []
    for canary in CANARIES:
        assert canary not in caplog.text
    assert "MUSIZEER_CREDENTIALS_KEY" in caplog.text, "and the operator is told why"


def test_the_unreadable_row_is_left_untouched_and_is_read_again_when_the_key_returns(engine, session, lost_key, monkeypatch):
    before = _data_column(engine)
    CredentialStore(session).get_api_key("quay")
    CredentialStore(session, user_id=lost_key).own_oauth2_token("orbit")

    assert _data_column(engine) == before, "reading never rewrites what it cannot read"

    use_keys(monkeypatch, KEY)
    assert CredentialStore(session).get_api_key("quay") == API_KEY
    assert CredentialStore(session, user_id=lost_key).own_oauth2_token("orbit").data["access_token"] == ACCESS


def test_connecting_again_replaces_what_could_not_be_read(session, lost_key):
    store = CredentialStore(session, user_id=lost_key)

    store.save_oauth2_token("orbit", {"access_token": "canary-reconnected"}, PERSONAL)
    CredentialStore(session).save_api_key("quay", "canary-new-key")

    assert store.own_oauth2_token("orbit").data == {"access_token": "canary-reconnected"}
    assert CredentialStore(session).get_api_key("quay") == "canary-new-key"


def test_a_refresh_is_never_attempted_over_a_row_that_could_not_be_read(session, lost_key):
    """`current` is None for the AuthManager: it has nothing to refresh, the account is not connected."""
    assert CredentialStore(session, user_id=lost_key).own_oauth2_token("orbit") is None


def test_deleting_an_unreadable_account_removes_it(engine, session, lost_key):
    CredentialStore(session, user_id=lost_key).delete_oauth2_token("orbit", PERSONAL)

    assert raw(engine, "SELECT 1 FROM provider_credentials WHERE user_id IS NOT NULL AND credential_type = 'oauth2_token'") == []


def test_disconnecting_one_utility_account_when_the_others_cannot_be_read_changes_nothing(engine, session, lost_key):
    store = CredentialStore(session)
    before = _data_column(engine)

    assert store.delete_external_utility_account("pier", "pier_cookies", "main") is False

    assert _data_column(engine) == before


def test_registering_a_utility_account_over_unreadable_ones_starts_again_from_it(session, lost_key):
    store = CredentialStore(session)

    store.save_external_utility_account("pier", "pier_cookies", "second", {"cookie": "canary-second"})

    assert store.get_external_utility_accounts("pier", "pier_cookies") == {"second": {"cookie": "canary-second"}}


def test_a_row_left_in_clear_is_not_returned_as_it_is(engine, session, caplog):
    """Only the migration reads a credential in clear; at run time it is as good as a lost one."""
    caplog.set_level(logging.WARNING)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO provider_credentials (id, provider, credential_type, data, updated_at) "
            "VALUES ('legacy', 'quay', 'api_key', '{\"api_key\": \"canary-in-clear\"}', '2026-01-01 00:00:00')"
        )

    assert CredentialStore(session).get_api_key("quay") is None
    assert "canary-in-clear" not in caplog.text and "in clear" in caplog.text


def test_a_tampered_value_is_not_returned(engine, session):
    CredentialStore(session).save_api_key("quay", API_KEY)
    (stored,) = _data_column(engine)
    tampered = stored[:-6] + ("AAAAAA" if not stored.endswith("AAAAAA") else "BBBBBB")
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE provider_credentials SET data = :data"), {"data": tampered})

    assert CredentialStore(session).get_api_key("quay") is None


def test_without_any_key_a_store_cannot_be_used_at_all(session, monkeypatch):
    from db.credential_cipher import CredentialsKeyError

    CredentialStore(session).save_api_key("quay", API_KEY)
    use_keys(monkeypatch, None)

    with pytest.raises(CredentialsKeyError):
        CredentialStore(session).get_api_key("quay")
    with pytest.raises(CredentialsKeyError):
        CredentialStore(session).save_api_key("quay", "x")
