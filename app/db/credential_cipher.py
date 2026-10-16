"""
The host side of the encryption of stored credentials (docs/credentials.md): where the key comes
from, the refusal to start without it, and the two operations the stores need (seal on a write,
unseal on a read).

The format and the cryptography are ``core/secret_box.py``. This module is the only one that reads
``CREDENTIALS_KEY`` (the key that seals) and ``CREDENTIALS_KEY_PREVIOUS`` (older keys, comma
separated, used only to open: what a rotation leaves behind, ``scripts/rotate_credentials_key.py``)
from the process environment. The provider gateway never has them: a provider receives the
credential it is to use for a call, never the key that sealed it.

What a value that cannot be opened does is decided by the store that read it (an account is "not
connected", a setting is "not set", never the text of the sealed value): ``unseal`` returns
``None`` for it and says so ONCE per distinct stored value, in a warning that names what it is and
never contains a value, a key or the sealed text.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading

from core.secret_box import InvalidKey, SecretBox, Unreadable, check_key, split_keys

logger = logging.getLogger(__name__)

ENV_KEY = "CREDENTIALS_KEY"
ENV_PREVIOUS = "CREDENTIALS_KEY_PREVIOUS"

_GENERATE = (
    'docker run --rm musizerr-app python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

# Warnings already given by this process, by (reason, digest of the stored text): a value read on
# every request is reported once, and a value that is replaced is a new one. Bounded.
_MAX_REPORTED = 2000
_reported: set[str] = set()
_reported_lock = threading.Lock()


class CredentialsKeyError(RuntimeError):
    """``CREDENTIALS_KEY`` cannot seal the stored credentials: it is missing, empty or not a key.
    The message says what is wrong and how to fix it, and never contains a key."""


# The box of the keys now in the environment, rebuilt when they change (a test sets them, a
# process never does): (material, box).
_current: tuple[tuple[str, str], SecretBox] | None = None


def box() -> SecretBox:
    """The box of the configured keys; ``CredentialsKeyError`` when they are not usable."""
    global _current
    material = (os.environ.get(ENV_KEY, ""), os.environ.get(ENV_PREVIOUS, ""))
    held = _current
    if held is not None and held[0] == material:
        return held[1]
    built = _build(*material)
    _current = (material, built)
    return built


def _build(primary: str, previous: str) -> SecretBox:
    primary = primary.strip()
    if not primary:
        raise CredentialsKeyError(
            f"{ENV_KEY} is not set or is empty. The credentials Musizerr stores (provider accounts, tokens, "
            f"API keys, secret settings, source tokens) are encrypted with it. Set {ENV_KEY} in .env to a "
            f"Fernet key, generated once with: {_GENERATE} . Keep it with the database backups: without it the "
            "stored credentials cannot be read and every account has to be connected again."
        )
    try:
        check_key(primary)
    except InvalidKey as invalid:
        raise CredentialsKeyError(
            f"{ENV_KEY} {invalid}. It must be a Fernet key: generate one with {_GENERATE} ."
        ) from None
    older = split_keys(previous)
    for position, key in enumerate(older, start=1):
        try:
            check_key(key)
        except InvalidKey as invalid:
            raise CredentialsKeyError(
                f"{ENV_PREVIOUS} (item {position} of {len(older)}) {invalid}. It lists the keys credentials "
                f"were encrypted with before, separated by commas."
            ) from None
    return SecretBox(primary, older)


def require_credentials_key() -> None:
    """Raise ``CredentialsKeyError`` unless the key is usable. The ONE check, with no switch to
    skip it: the application, every worker and beat ask it before touching the database, and the
    migration that encrypts the existing credentials asks it before it changes a row."""
    box()


def seal(plaintext: str) -> str:
    """``plaintext`` sealed with the primary key."""
    return box().seal(plaintext)


def unseal(stored: str, what: str) -> str | None:
    """The plaintext of ``stored``, or None when it cannot be read: it is not sealed (only the
    migration accepts a value in clear) or no configured key opens it. ``what`` names the value for
    the warning (``"oauth2_token of provider 'x'"``): a description, never a value. A missing or
    malformed key is not "unreadable" but a misconfiguration: ``CredentialsKeyError``."""
    try:
        return box().open(stored)
    except Unreadable as unreadable:
        _report(what, stored, unreadable.reason)
        return None


def _report(what: str, stored: str, reason: str) -> None:
    mark = f"{reason}:{hashlib.sha256(stored.encode('utf-8', 'replace')).hexdigest()[:16]}"
    with _reported_lock:
        if mark in _reported:
            return
        if len(_reported) >= _MAX_REPORTED:
            _reported.clear()
        _reported.add(mark)
    cause = (
        "it is stored in clear, which only the migration that encrypts credentials accepts"
        if reason == Unreadable.PLAINTEXT
        else f"it was encrypted with a key that is not configured (or was altered); the keys now are {ENV_KEY} and {ENV_PREVIOUS}"
    )
    logger.warning(
        "%s cannot be read: %s. It is treated as not set: restore the key it was encrypted with, "
        "or connect it / enter it again (docs/credentials.md).",
        what, cause,
    )


def forget_reports() -> None:
    """Forget the warnings given (tests)."""
    with _reported_lock:
        _reported.clear()
