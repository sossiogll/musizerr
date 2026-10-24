"""
Handler degli errori che non sono colpa dell'host, installati sull'app da
``main.py`` (e dai test che montano un router da solo):

- :class:`services.provider_calls.ProviderCallError` — il provider di una
  rotta di dettaglio ha fallito: 404 se dice che la risorsa non esiste, 503
  (con ``Retry-After``) se è irraggiungibile, lento, limitato o non
  configurato, 502 altrimenti. Corpo ``{"detail": {error_code,
  error_message, provider[, error_detail]}}``; ``error_detail`` (il testo
  grezzo) solo per un amministratore.
- :class:`services.folders.FoldersNotReady` — a folder the product needs is not set (ADR-0008): 409 with
  ``error_code`` ``working_folder_not_set`` or ``library_folder_not_set``; ``error_detail`` (why: a root the gateway
  refuses, a value that could not be converted) only for an administrator.
- un lock del database scaduto (SQLite ``database is locked``; PostgreSQL
  lock timeout, deadlock, conflitto di serializzazione, statement timeout) —
  503 riprovabile con ``error_code: database_busy``, non un 500.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import OperationalError

from routes import deps
from services.errors import public_error
from services.folders import FoldersNotReady
from services.provider_calls import ProviderCallError

logger = logging.getLogger(__name__)

PROVIDER_RETRY_AFTER_S = 30
# A provider that says how long its rate limit lasts is believed, up to this
# (an exhausted daily quota can ask for many hours: a client should poll again
# before that, not hold a request for a day).
PROVIDER_RETRY_AFTER_MAX_S = 3600
DATABASE_RETRY_AFTER_S = 2

_SQLITE_LOCK_MARKERS = ("database is locked", "database table is locked", "database is busy")
# PostgreSQL SQLSTATE: lock_not_available (lock_timeout), deadlock_detected,
# serialization_failure, query_canceled (statement_timeout). The same request
# a moment later goes through.
_POSTGRESQL_BUSY_STATES = frozenset({"55P03", "40P01", "40001", "57014"})


def is_lock_timeout(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    return (
        getattr(exc.orig, "sqlstate", None) in _POSTGRESQL_BUSY_STATES
        or any(marker in str(exc).lower() for marker in _SQLITE_LOCK_MARKERS)
    )


def _resolve(request: Request, dependency):
    """La dipendenza (senza argomenti) come la vedrebbe una rotta, compresi
    gli override dei test."""
    return request.app.dependency_overrides.get(dependency, dependency)()


def _viewer_is_admin(request: Request) -> bool:
    """Chi guarda è un amministratore? Calcolato solo quando serve (una
    risposta d'errore), con le stesse regole di ``get_optional_user``."""
    try:
        config = _resolve(request, deps.get_config)
        if not config.auth_enabled:
            return True
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        sessions = _resolve(request, deps.get_session)
        session = next(sessions)
        try:
            user = deps.get_optional_user(
                session, config, HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            )
        finally:
            sessions.close()
        return user is not None and user.role == "admin"
    except Exception:
        # Nel dubbio il testo grezzo non si mostra.
        return False


def provider_retry_after(error: BaseException) -> int:
    """Seconds for the ``Retry-After`` of a retryable provider failure: the
    ``retry_after`` the provider or its upstream gave (a ``ProviderRateLimited``,
    or a failure the runner classified), else the default."""
    seconds = getattr(error, "retry_after", None)
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return PROVIDER_RETRY_AFTER_S
    return min(math.ceil(seconds), PROVIDER_RETRY_AFTER_MAX_S)


async def _provider_call_error(request: Request, exc: ProviderCallError) -> JSONResponse:
    status = exc.status_code
    if exc.kind == "not_found":
        return JSONResponse(status_code=404, content={"detail": "Not found on the provider"})
    show_detail = await run_in_threadpool(_viewer_is_admin, request)
    body: dict[str, Any] = {**public_error(exc.error, exc.subject, show_detail=show_detail), "provider": exc.provider}
    headers = {"Retry-After": str(provider_retry_after(exc.error))} if status == 503 else None
    return JSONResponse(status_code=status, content={"detail": body}, headers=headers)


async def _operational_error(request: Request, exc: OperationalError) -> JSONResponse:
    if is_lock_timeout(exc):
        logger.warning("%s %s: database busy (%s)", request.method, request.url.path, exc.orig)
        return JSONResponse(
            status_code=503,
            content={"detail": {
                "error_code": "database_busy",
                "error_message": "The server is busy, try again in a moment.",
            }},
            headers={"Retry-After": str(DATABASE_RETRY_AFTER_S)},
        )
    logger.error("%s %s: database error", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


async def _folders_not_ready(request: Request, exc: FoldersNotReady) -> JSONResponse:
    body: dict[str, Any] = {"error_code": exc.error_code, "error_message": exc.message}
    if exc.detail and await run_in_threadpool(_viewer_is_admin, request):
        body["error_detail"] = exc.detail
    return JSONResponse(status_code=409, content={"detail": body})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProviderCallError, _provider_call_error)
    app.add_exception_handler(FoldersNotReady, _folders_not_ready)
    app.add_exception_handler(OperationalError, _operational_error)
