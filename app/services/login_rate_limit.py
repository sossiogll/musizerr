"""
A fixed-window limit on the routes that sign someone in through a provider
(``routes/provider_login.py``), counted per client address in Redis so it holds
across workers.

The routes that answer an authorization or a login form each cost the provider
a call (the code exchange, the check of the credentials): without a limit this
host would be a free way to guess another service's credentials. Without Redis
nothing is limited (the same stance as ``services/provider_cooldown.py``): a
missing counter must not lock everybody out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import redis
from fastapi import HTTPException, Request

from core.provider_login import ERROR_RATE_LIMITED
from services.progress import REDIS_URL

logger = logging.getLogger(__name__)

# Requests per client and window across the sign-in routes: far above a person
# fumbling a password, far below a guessing run.
MAX_ATTEMPTS = 20
WINDOW_SECONDS = 60

_shared_client = None


def _client():
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis.from_url(REDIS_URL)
    return _shared_client


def client_address(request: Request) -> str:
    """The address the request came from. Behind the reverse proxy it is the
    last entry of ``X-Forwarded-For`` (the one the proxy appended: what a
    client wrote before it is never trusted)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    return last or (request.client.host if request.client else "unknown")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    retry_after: int = 0


def hit(address: str, *, client=None, max_attempts: int | None = None, window: int | None = None) -> Verdict:
    """Counts one request of ``address``; not allowed once ``max_attempts`` are spent in the window."""
    max_attempts = MAX_ATTEMPTS if max_attempts is None else max_attempts
    window = WINDOW_SECONDS if window is None else window
    client = client if client is not None else _client()
    key = f"musizeer:provider-login:{address}"
    try:
        count = int(client.incr(key))
        ttl = int(client.ttl(key))
        if count == 1 or ttl < 0:  # a counter without an expiry would block forever
            client.expire(key, window)
            ttl = window
    except Exception as exc:  # noqa: BLE001 — without Redis nobody is limited
        logger.debug("sign-in rate limit unavailable: %s", exc)
        return Verdict(True)
    return Verdict(count <= max_attempts, max(ttl, 1))


def limit_provider_login(request: Request) -> None:
    """FastAPI dependency of every sign-in route: 429 with ``Retry-After``."""
    verdict = hit(client_address(request))
    if not verdict.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error_code": ERROR_RATE_LIMITED, "error_message": "Too many sign-in attempts, try again shortly."},
            headers={"Retry-After": str(verdict.retry_after)},
        )
