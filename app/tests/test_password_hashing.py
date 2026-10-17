"""Password hashing: bcrypt reads 72 bytes; a longer password must neither fail nor lock its user out."""
from __future__ import annotations

import bcrypt

from core import user_auth


def test_a_password_round_trips():
    assert user_auth.verify_password("correct horse", user_auth.hash_password("correct horse"))
    assert not user_auth.verify_password("wrong horse", user_auth.hash_password("correct horse"))


def test_a_password_longer_than_72_bytes_is_hashed_and_verified():
    long_password = "p" * 100
    hashed = user_auth.hash_password(long_password)
    assert user_auth.verify_password(long_password, hashed)


def test_a_hash_stored_before_bcrypt_5_still_verifies():
    """bcrypt 4 cut the password at 72 bytes by itself: that is the hash it stored."""
    long_password = "é" * 50  # 100 bytes
    stored = bcrypt.hashpw(long_password.encode()[:72], bcrypt.gensalt(4)).decode()
    assert user_auth.verify_password(long_password, stored)
