"""
``core/secret_box.py``: the format of a sealed value and the cryptography under it. Pure: no
environment, no database. What the host does with a value that cannot be opened is the stores' (see
``test_credential_store_encryption.py``).
"""
from __future__ import annotations

import base64

import pytest
from hypothesis import given, settings as hypothesis_settings, strategies as st

from core import secret_box
from core.secret_box import InvalidKey, SecretBox, Unreadable

from tests.credentials_support import KEY, OTHER_KEY, THIRD_KEY


def test_a_value_round_trips_and_is_not_its_plaintext():
    box = SecretBox(KEY)

    sealed = box.seal('{"access_token": "canary-token-123"}')

    assert sealed.startswith("enc:v1:")
    assert "canary-token-123" not in sealed
    assert box.open(sealed) == '{"access_token": "canary-token-123"}'


@given(st.text())
@hypothesis_settings(max_examples=60, deadline=None)
def test_any_text_round_trips(text):
    box = SecretBox(KEY)

    assert box.open(box.seal(text)) == text


def test_the_same_plaintext_never_seals_to_the_same_text_twice():
    box = SecretBox(KEY)

    assert box.seal("same") != box.seal("same")


def test_unicode_and_empty_text_round_trip():
    box = SecretBox(KEY)

    assert box.open(box.seal("")) == ""
    assert box.open(box.seal("pässwörd 🎵 日本")) == "pässwörd 🎵 日本"


def test_a_wrong_key_cannot_open_a_value():
    sealed = SecretBox(KEY).seal("secret")

    with pytest.raises(Unreadable) as refused:
        SecretBox(OTHER_KEY).open(sealed)

    assert refused.value.reason == Unreadable.UNDECRYPTABLE
    assert "secret" not in str(refused.value)


def test_a_value_in_clear_is_unreadable_not_returned_as_it_is():
    with pytest.raises(Unreadable) as refused:
        SecretBox(KEY).open('{"api_key": "in-clear"}')

    assert refused.value.reason == Unreadable.PLAINTEXT


@pytest.mark.parametrize("position", [0, 10, -5])
def test_an_altered_value_is_refused(position):
    box = SecretBox(KEY)
    sealed = box.seal("secret")
    body = sealed[len("enc:v1:"):]
    index = position % len(body)
    flipped = body[:index] + ("A" if body[index] != "A" else "B") + body[index + 1:]

    with pytest.raises(Unreadable) as refused:
        box.open("enc:v1:" + flipped)

    assert refused.value.reason == Unreadable.UNDECRYPTABLE


@pytest.mark.parametrize("stored", ["enc:v1:", "enc:v1:not-a-token", "enc:v1:" + "A" * 10, "enc:v1:éé"])
def test_a_malformed_sealed_value_is_unreadable(stored):
    with pytest.raises(Unreadable):
        SecretBox(KEY).open(stored)


def test_a_truncated_value_is_refused():
    box = SecretBox(KEY)
    sealed = box.seal("secret")

    with pytest.raises(Unreadable):
        box.open(sealed[:-8])


def test_previous_keys_open_what_they_sealed_and_the_primary_seals():
    old = SecretBox(OTHER_KEY).seal("secret")
    box = SecretBox(KEY, [OTHER_KEY])

    assert box.open(old) == "secret"
    assert not box.is_current(old)
    with pytest.raises(Unreadable):
        SecretBox(OTHER_KEY).open(box.seal("x"))  # what the box seals, the old key cannot open


def test_resealing_moves_a_value_to_the_primary_key():
    old = SecretBox(OTHER_KEY).seal("secret")
    box = SecretBox(KEY, [OTHER_KEY])

    fresh = box.reseal(old)

    assert box.is_current(fresh)
    assert SecretBox(KEY).open(fresh) == "secret", "the old key is no longer needed"


def test_resealing_what_no_key_opens_is_refused():
    with pytest.raises(Unreadable):
        SecretBox(KEY, [OTHER_KEY]).reseal(SecretBox(THIRD_KEY).seal("secret"))


def test_is_current_is_false_for_a_value_in_clear():
    assert SecretBox(KEY).is_current("plain") is False


def test_a_duplicated_previous_key_is_harmless():
    box = SecretBox(KEY, [KEY, OTHER_KEY, OTHER_KEY])

    assert box.open(SecretBox(OTHER_KEY).seal("a")) == "a"


# ---------------------------------------------------------------------------
# The keys
# ---------------------------------------------------------------------------


def test_a_generated_key_is_valid():
    from cryptography.fernet import Fernet

    secret_box.check_key(Fernet.generate_key().decode())
    SecretBox(Fernet.generate_key().decode())


@pytest.mark.parametrize("key", [KEY, OTHER_KEY, THIRD_KEY, f"  {KEY}  "])
def test_valid_keys_are_accepted(key):
    secret_box.check_key(key)


@pytest.mark.parametrize(
    "key, why",
    [
        pytest.param("", "empty", id="empty"),
        pytest.param("   ", "empty", id="blank"),
        pytest.param("change-me-in-production", "base64", id="the-auth-secret-placeholder"),
        pytest.param("secret", "base64", id="short-word"),
        pytest.param(KEY[:-1], "base64", id="padding-removed"),
        pytest.param(KEY[:-4] + "====", "base64", id="wrong-padding"),
        pytest.param(base64.urlsafe_b64encode(b"x" * 16).decode(), "32 bytes", id="16-bytes"),
        pytest.param(base64.urlsafe_b64encode(b"y" * 33 + b"z").decode(), "32 bytes", id="34-bytes"),
        pytest.param("A" * 43 + "=", "placeholder", id="all-zero-key"),
        pytest.param(base64.urlsafe_b64encode(b"password" * 4).decode(), "placeholder", id="a-word-repeated"),
        pytest.param("é" * 44, "base64", id="not-ascii"),
    ],
)
def test_a_bad_key_is_refused_with_a_reason_that_does_not_contain_it(key, why):
    with pytest.raises(InvalidKey) as refused:
        secret_box.check_key(key)

    assert why in str(refused.value)
    if key.strip():
        assert key.strip() not in str(refused.value)


def test_a_box_refuses_a_bad_previous_key():
    with pytest.raises(InvalidKey):
        SecretBox(KEY, ["not-a-key"])


def test_split_keys():
    assert secret_box.split_keys(None) == []
    assert secret_box.split_keys("") == []
    assert secret_box.split_keys(f" {KEY} , ,{OTHER_KEY},") == [KEY, OTHER_KEY]


# ---------------------------------------------------------------------------
# The envelope of a sealed setting
# ---------------------------------------------------------------------------


def test_an_envelope_is_recognised_only_when_it_is_exactly_one():
    token = SecretBox(KEY).seal("1")

    assert secret_box.enveloped_token(secret_box.envelope(token)) == token
    assert secret_box.enveloped_token(token) is None, "a string that looks sealed is just a string"
    assert secret_box.enveloped_token({"$sealed": token, "extra": 1}) is None
    assert secret_box.enveloped_token({"$sealed": "not sealed"}) is None
    assert secret_box.enveloped_token(["$sealed"]) is None
    assert secret_box.enveloped_token(None) is None
