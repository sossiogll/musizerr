from __future__ import annotations

from typing import Annotated, Optional

import jwt
from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import text

from core import user_auth
from db.engine import engine
from services import app_version
from services.schema_status import schema_status
from routes.deps import ConfigDep, get_registry

router = APIRouter()

_bearer = HTTPBearer(auto_error=False)


def _token_is_admin(credentials: HTTPAuthorizationCredentials | None) -> bool:
    """Ruolo admin letto dal token di accesso firmato, senza il DB: /health
    deve rispondere anche quando il DB non risponde."""
    if credentials is None:
        return False
    try:
        return user_auth.decode_access_token(credentials.credentials).get("role") == "admin"
    except jwt.PyJWTError:
        return False


@router.get("/health")
def health(
    config: ConfigDep,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)] = None,
):
    """Stato del servizio e del DB per chiunque; l'elenco dei provider e il
    dettaglio di un errore del DB solo per gli admin (o con
    l'autenticazione disattivata)."""
    is_admin = not config.auth_enabled or _token_is_admin(credentials)
    status: dict = {"status": "ok", "db": "ok"}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        status["status"] = "degraded"
        status["db"] = "error"
        if is_admin:
            status["db_detail"] = str(exc)

    if is_admin:
        # The version that runs (ADR-0007 §2): admins only, like the providers. Telling everyone would
        # tell who looks for a vulnerable version which instances run it.
        status["version"], status["commit"] = app_version.VERSION, app_version.COMMIT
        # The schema the database is at (ADR-0007 §2), when it can be read: not while the database is down.
        schema = schema_status(engine) if status["db"] == "ok" else None
        if schema is not None:
            status["schema"] = schema
        try:
            registry = get_registry()
            status["providers"] = {
                "metadata": registry.metadata_names(),
                "download": registry.download_names(),
            }
        except Exception:
            status["providers"] = "not initialized"

    return status
