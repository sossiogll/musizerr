"""The password rule as the routes answer it: every route that sets a password goes through here."""
from __future__ import annotations

from fastapi import HTTPException

from core import user_auth


def new_password_hash(plain: str) -> str:
    """The hash of a password being set, or a 422 ``password_too_short`` (a code the UI translates)."""
    try:
        return user_auth.hash_new_password(plain)
    except user_auth.WeakPassword as weak:
        raise HTTPException(
            status_code=422,
            detail={"error_code": weak.error_code, "error_message": str(weak), "min_length": user_auth.MIN_PASSWORD_LENGTH},
        ) from None
