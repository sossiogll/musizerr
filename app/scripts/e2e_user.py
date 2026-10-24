"""
The accounts the end-to-end smoke suite signs in with (``e2e/README.md``): a
plain, active user named ``E2E_USERNAME`` whose password is set to
``E2E_PASSWORD`` on every run and, when ``E2E_ADMIN_PASSWORD`` is given, an
administrator named ``E2E_ADMIN_USERNAME`` (the specs that decide a user's
download request need one). For a development instance only.

Uso: ``python -m scripts.e2e_user`` dalla cartella ``app/`` (``e2e/run.sh``
lo lancia nel container dell'API).
"""
from __future__ import annotations

import sys

from sqlmodel import Session

from core import environment, user_auth
from db import user_service
from db.engine import engine


def ensure_account(session: Session, username: str, password: str, role: str) -> None:
    """Creates ``username`` with ``role``, or resets an existing one: active, that
    role, that password."""
    user = user_service.get_user_by_username(session, username)
    password_hash = user_auth.hash_password(password)
    if user is None:
        user_service.create_user(session, username=username, password_hash=password_hash, role=role)
        print(f"e2e {role} account '{username}' created")
    else:
        user_service.update_user(session, user, password_hash=password_hash, is_active=True, role=role)
        print(f"e2e {role} account '{username}' reset")


def main() -> int:
    username = environment.get("E2E_USERNAME")
    password = environment.get("E2E_PASSWORD")
    if not password:
        print("E2E_PASSWORD is not set", file=sys.stderr)
        return 2
    admin_password = environment.get("E2E_ADMIN_PASSWORD")
    with Session(engine) as session:
        ensure_account(session, username, password, "user")
        if admin_password:
            ensure_account(session, environment.get("E2E_ADMIN_USERNAME"), admin_password, "admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
