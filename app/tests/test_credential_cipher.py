"""
``db/credential_cipher.py``: where the key comes from, the refusal to run without a usable one, and
the warning a value that cannot be opened is reported with (once, and without any secret in it).
"""
from __future__ import annotations

import logging

import pytest

from core.secret_box import SecretBox
from db import credential_cipher
from db.credential_cipher import CredentialsKeyError

from tests.credentials_support import KEY, OTHER_KEY, THIRD_KEY, use_keys

REFUSED = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("change-me-in-production", id="a-placeholder"),
    pytest.param("secret", id="a-word"),
    pytest.param("0123456789abcdef" * 4, id="an-openssl-rand-hex-32-is-not-a-fernet-key"),
    pytest.param(KEY[:-1], id="truncated"),
    pytest.param("A" * 43 + "=", id="all-zero-key"),
]


@pytest.mark.parametrize("key", REFUSED)
def test_an_unusable_key_is_refused_with_the_way_to_make_one(key, monkeypatch):
    use_keys(monkeypatch, key)

    with pytest.raises(CredentialsKeyError) as refused:
        credential_cipher.require_credentials_key()

    message = str(refused.value)
    assert "CREDENTIALS_KEY" in message and "Fernet.generate_key()" in message
    if key and len(key.strip()) > 8:
        assert key.strip() not in message, "the value is never echoed"


def test_a_usable_key_is_accepted(monkeypatch):
    use_keys(monkeypatch, OTHER_KEY)

    credential_cipher.require_credentials_key()


def test_a_bad_previous_key_is_refused_and_named(monkeypatch):
    use_keys(monkeypatch, KEY, OTHER_KEY, "nonsense-key-value")

    with pytest.raises(CredentialsKeyError) as refused:
        credential_cipher.require_credentials_key()

    assert "CREDENTIALS_KEY_PREVIOUS" in str(refused.value) and "item 2 of 2" in str(refused.value)
    assert "nonsense-key-value" not in str(refused.value)


def test_the_previous_keys_are_optional(monkeypatch):
    use_keys(monkeypatch, KEY)
    monkeypatch.setenv("CREDENTIALS_KEY_PREVIOUS", "  ")

    credential_cipher.require_credentials_key()


def test_the_box_follows_the_environment(monkeypatch):
    use_keys(monkeypatch, KEY)
    sealed = credential_cipher.seal("secret")
    assert credential_cipher.unseal(sealed, "a value") == "secret"

    use_keys(monkeypatch, OTHER_KEY)
    assert credential_cipher.unseal(sealed, "a value") is None, "another key cannot open it"

    use_keys(monkeypatch, OTHER_KEY, KEY)
    assert credential_cipher.unseal(sealed, "a value") == "secret", "the old key, kept as previous, can"
    assert SecretBox(OTHER_KEY).open(credential_cipher.seal("x")) == "x", "and the new one seals"


def test_without_a_key_nothing_is_sealed_or_opened(monkeypatch):
    use_keys(monkeypatch, None)

    with pytest.raises(CredentialsKeyError):
        credential_cipher.seal("secret")
    with pytest.raises(CredentialsKeyError):
        credential_cipher.unseal("enc:v1:whatever", "a value")


def test_an_unreadable_value_is_reported_once_without_any_secret(caplog):
    sealed = SecretBox(THIRD_KEY).seal("canary-plaintext")
    caplog.set_level(logging.DEBUG)

    for _ in range(5):
        assert credential_cipher.unseal(sealed, "The api_key credential of provider 'orbit' (shared)") is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "provider 'orbit'" in text and "CREDENTIALS_KEY" in text
    for forbidden in ("canary-plaintext", sealed, sealed.split(":")[-1], KEY, OTHER_KEY, THIRD_KEY):
        assert forbidden not in text and forbidden not in caplog.text


def test_a_different_unreadable_value_is_reported_again(caplog):
    caplog.set_level(logging.WARNING)

    credential_cipher.unseal(SecretBox(THIRD_KEY).seal("one"), "first")
    credential_cipher.unseal(SecretBox(THIRD_KEY).seal("two"), "second")

    assert [r.getMessage().split(" cannot")[0] for r in caplog.records] == ["first", "second"]


def test_a_value_in_clear_is_unreadable_and_says_why(caplog):
    caplog.set_level(logging.WARNING)

    assert credential_cipher.unseal('{"api_key": "canary-in-clear"}', "The api_key credential") is None

    assert "in clear" in caplog.text and "canary-in-clear" not in caplog.text
