"""
Authenticated encryption of the secrets Musizeer keeps in its database (docs/credentials.md).

PURE: the keys come in as arguments, nothing is read from the environment and nothing is logged
here; ``db/credential_cipher.py`` is the host module that owns the environment variables, the
startup refusal and the warnings. This module owns the FORMAT, so that everything that writes or
reads a sealed value (the stores, the migration, the rotation command) agrees on it.

A sealed value is ``enc:v1:`` followed by a Fernet token (``cryptography``: AES-128-CBC with an
HMAC-SHA256 over the whole token, so a wrong key and an altered value are both refused, and the
token carries a random IV, so the same plaintext never seals to the same text twice). The prefix
tells a sealed value from a legacy one in clear and carries the version of the format: a new
scheme is ``enc:v2:`` and is read next to this one. A key is a Fernet key: the urlsafe base64 of
32 random bytes (``Fernet.generate_key()``).

Several keys may be given: the first one seals; every one of them opens (``MultiFernet``), which is
how a key is rotated without a gap (``rotate_credentials_key``).
"""
from __future__ import annotations

import base64
import binascii
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PREFIX = "enc:v1:"
KEY_BYTES = 32
# A random key of 32 bytes has about 31 distinct byte values; a key made of one repeated byte (all
# zeros, the base64 of a repeated character) is a placeholder anyone can type, not a secret.
MIN_DISTINCT_KEY_BYTES = 16


class InvalidKey(ValueError):
    """A key that cannot be used. The message says why and never contains the key."""


class Unreadable(Exception):
    """A stored value that cannot be turned into its plaintext. ``reason`` is ``plaintext`` (it is
    not in the sealed form) or ``undecryptable`` (sealed with a key that is not configured, or
    altered: the two cannot be told apart). Never carries the value."""

    PLAINTEXT = "plaintext"
    UNDECRYPTABLE = "undecryptable"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_sealed(stored: str | None) -> bool:
    """Whether ``stored`` is in the sealed form (it says nothing of whether it can be opened)."""
    return isinstance(stored, str) and stored.startswith(PREFIX)


def split_keys(text: str | None) -> list[str]:
    """The keys of a comma-separated list (``CREDENTIALS_KEY_PREVIOUS``): spaces around a key and
    empty items are ignored."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def check_key(text: str) -> bytes:
    """The key as bytes, or ``InvalidKey``: urlsafe base64 (with padding) of exactly ``KEY_BYTES``
    bytes, and not a repeated byte."""
    candidate = (text or "").strip()
    if not candidate:
        raise InvalidKey("is empty")
    try:
        raw = base64.urlsafe_b64decode(candidate.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise InvalidKey("is not urlsafe base64") from None
    if len(raw) != KEY_BYTES or base64.urlsafe_b64encode(raw).decode("ascii") != candidate:
        raise InvalidKey(f"is not the urlsafe base64 of {KEY_BYTES} bytes (a Fernet key is 44 characters ending in '=')")
    if len(set(raw)) < MIN_DISTINCT_KEY_BYTES:
        raise InvalidKey("looks like a placeholder (a repeated byte), not a random key")
    return candidate.encode("ascii")


class SecretBox:
    """Seals with the first key and opens with any of them."""

    def __init__(self, primary: str, previous: Iterable[str] = ()) -> None:
        keys = [check_key(primary)]
        for old in previous:
            key = check_key(old)
            if key not in keys:
                keys.append(key)
        self._primary = Fernet(keys[0])
        self._all = MultiFernet([Fernet(key) for key in keys])

    def seal(self, plaintext: str) -> str:
        return PREFIX + self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def open(self, stored: str) -> str:
        """The plaintext of a sealed value; ``Unreadable`` when it is not sealed or no key opens it."""
        if not is_sealed(stored):
            raise Unreadable(Unreadable.PLAINTEXT)
        try:
            return self._all.decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise Unreadable(Unreadable.UNDECRYPTABLE) from None

    def is_current(self, stored: str) -> bool:
        """Whether a sealed value is already sealed with the primary key."""
        if not is_sealed(stored):
            return False
        try:
            self._primary.decrypt(stored[len(PREFIX):].encode("ascii"))
        except (InvalidToken, UnicodeError):
            return False
        return True

    def reseal(self, stored: str) -> str:
        """A sealed value sealed again with the primary key (read with any key); ``Unreadable``
        when no key opens it."""
        return self.seal(self.open(stored))


# ---------------------------------------------------------------------------
# A sealed value inside a JSON document (the ``settings`` table keeps every value as JSON)
# ---------------------------------------------------------------------------

# A sealed setting is ``{"$sealed": "enc:v1:..."}``: no setting has an object for a value (the
# types are bool, int, float, string, enum, list and provider_ref), so a value that is exactly this
# cannot be anything else, and a string setting that happens to begin with ``enc:v1:`` is still just
# a string.
ENVELOPE_KEY = "$sealed"


def envelope(token: str) -> dict[str, str]:
    """The JSON value that holds the sealed ``token`` of a setting."""
    return {ENVELOPE_KEY: token}


def enveloped_token(document: object) -> str | None:
    """The sealed token inside a JSON value that is a sealed-setting envelope, else None."""
    if isinstance(document, dict) and set(document) == {ENVELOPE_KEY} and is_sealed(document[ENVELOPE_KEY]):
        return document[ENVELOPE_KEY]
    return None
