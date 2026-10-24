"""
Keys and helpers for the tests of the encryption of stored credentials (docs/credentials.md).

The keys are FIXED and public: they exist for the tests only and seal nothing real. ``tests/conftest.py``
sets ``KEY`` as the ``MUSIZEER_CREDENTIALS_KEY`` of every test (the way the suite is given ``MUSIZEER_AUTH_SECRET``), so a
store can seal and open without each test arranging it; a test about the keys sets its own with
``monkeypatch`` (``use_keys``).
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

# Three different, valid Fernet keys (urlsafe base64 of 32 random bytes).
KEY = "3ER8zf6Vc8gFp5cbk2OqSEx_sA1KfLVT3q0ByU7bFHQ="
OTHER_KEY = "QlIvO27IC3ddbXQSraylSHJ0xxUebTmKBFr-3F3fuAI="
THIRD_KEY = "NnxeDSgU3KZusKJOm9aEZe1u0B0Syd8YTs9iGn5G6d4="


def use_keys(monkeypatch: pytest.MonkeyPatch, primary: str | None, *previous: str) -> None:
    """The process's ``MUSIZEER_CREDENTIALS_KEY`` / ``MUSIZEER_CREDENTIALS_KEY_PREVIOUS`` for the rest of the test
    (``None``: unset)."""
    if primary is None:
        monkeypatch.delenv("MUSIZEER_CREDENTIALS_KEY", raising=False)
    else:
        monkeypatch.setenv("MUSIZEER_CREDENTIALS_KEY", primary)
    if previous:
        monkeypatch.setenv("MUSIZEER_CREDENTIALS_KEY_PREVIOUS", ",".join(previous))
    else:
        monkeypatch.delenv("MUSIZEER_CREDENTIALS_KEY_PREVIOUS", raising=False)


def raw(engine, statement: str, **params):
    """All the rows of a raw SELECT: what is REALLY in the database, under the stores."""
    with engine.connect() as connection:
        return connection.execute(sa.text(statement), params).all()


def raw_scalar(engine, statement: str, **params):
    rows = raw(engine, statement, **params)
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    return rows[0][0]


def assert_sealed(text: str) -> None:
    """A stored text is in the sealed form: not the plaintext, and a Fernet token after the prefix."""
    from core import secret_box

    assert secret_box.is_sealed(text), "the value is stored in clear"
