"""
How a failure of a provider call is told apart, in the one place that can see
the exception types: next to the provider (the runner), not in the host, where
a provider's HTTP library does not exist.

``classify_exception`` is the type-based half of the host's classification (the
text-based half, for what has no recognisable type, stays in the host: it is
policy). It uses the standard library and duck typing only, so an error of
httpx, requests or urllib classifies the same without this package importing
any of them:

- a timeout: ``TimeoutError`` (also ``socket.timeout``), or a class named
  ``TimeoutException`` / ``Timeout`` anywhere in its hierarchy;
- an HTTP status: an exception carrying ``response.status_code`` (httpx's
  ``HTTPStatusError``, requests' ``HTTPError``) or, for urllib's ``HTTPError``,
  ``code``: 401/403 ``auth_failed``, 404 ``not_found``, 429 ``rate_limited``,
  5xx ``upstream_error``, any other ``failed``;
- the network: ``ConnectionError`` or a class named ``ConnectError`` /
  ``NetworkError`` / ``ConnectionError`` / ``URLError``, and ``socket.gaierror``:
  ``unreachable``;
- the sandbox's egress proxy: an answer it gives itself carries
  ``EGRESS_HEADER`` (``unreachable``: the destination did not answer;
  ``refused``: the provider may not reach it), and an HTTPS tunnel it does not
  open is a class named ``ProxyError`` whose text starts with the proxy's status.
  A destination that does not answer is ``unreachable``, never the 502 of an
  upstream that answered; a refused one is ``failed``.

What none of these recognises has no ``kind`` (``None``): the host reads its
message.
"""
from __future__ import annotations

import math
import socket
from dataclasses import dataclass

from .errors import ProviderRateLimited

KIND_TIMEOUT = "timeout"
KIND_UNREACHABLE = "unreachable"
KIND_AUTH_FAILED = "auth_failed"
KIND_NOT_FOUND = "not_found"
KIND_RATE_LIMITED = "rate_limited"
KIND_UPSTREAM_ERROR = "upstream_error"
KIND_FAILED = "failed"

# The kinds a classification can give (the host adds its own, e.g. "declined"
# and "not_configured", from what it knows).
KINDS = (
    KIND_TIMEOUT, KIND_UNREACHABLE, KIND_AUTH_FAILED, KIND_NOT_FOUND, KIND_RATE_LIMITED, KIND_UPSTREAM_ERROR,
    KIND_FAILED,
)

_TIMEOUT_NAMES = frozenset({"TimeoutException", "Timeout"})
_NETWORK_NAMES = frozenset({"ConnectError", "NetworkError", "ConnectionError", "URLError"})

# The header the sandbox's egress proxy puts on the answers it gives itself,
# and its values: the provider's HTTP library sees them as an ordinary
# response, the classifier as what they are.
EGRESS_HEADER = "X-Musizeer-Egress"
EGRESS_UNREACHABLE = "unreachable"
EGRESS_REFUSED = "refused"


@dataclass(frozen=True)
class Failure:
    """What the host is told about a failed call. ``kind`` is ``None`` when the
    exception's type says nothing; ``retry_after`` (seconds) only when the
    upstream said how long to wait."""

    kind: str | None
    message: str
    retry_after: float | None = None


def _hierarchy(exc: BaseException) -> frozenset[str]:
    return frozenset(cls.__name__ for cls in type(exc).__mro__)


def _status(exc: BaseException, names: frozenset[str]) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None and "HTTPError" in names:
        status = getattr(exc, "code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _retry_after(exc: BaseException) -> float | None:
    """The seconds of a numeric ``Retry-After`` header of the response."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _egress_verdict(exc: BaseException, names: frozenset[str], message: str) -> str | None:
    """What the egress proxy said, when the failure is its own answer: its
    header on a plain-HTTP response, or the status at the start of the text of
    an HTTPS tunnel it did not open."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None and hasattr(headers, "get"):
        verdict = headers.get(EGRESS_HEADER) or headers.get(EGRESS_HEADER.lower())
        if verdict in (EGRESS_UNREACHABLE, EGRESS_REFUSED):
            return verdict
    if "ProxyError" in names:
        return EGRESS_REFUSED if message.lstrip().startswith("403") else EGRESS_UNREACHABLE
    return None


def classify_exception(exc: BaseException) -> Failure:
    message = str(exc) or type(exc).__name__
    if isinstance(exc, ProviderRateLimited):
        return Failure(KIND_RATE_LIMITED, message, exc.retry_after)
    names = _hierarchy(exc)
    reason = getattr(exc, "reason", None) if "URLError" in names else None
    if isinstance(exc, TimeoutError) or names & _TIMEOUT_NAMES or isinstance(reason, TimeoutError):
        return Failure(KIND_TIMEOUT, message)
    verdict = _egress_verdict(exc, names, message)
    if verdict == EGRESS_UNREACHABLE:
        return Failure(KIND_UNREACHABLE, message)
    if verdict == EGRESS_REFUSED:
        return Failure(KIND_FAILED, message)
    status = _status(exc, names)
    if status is not None:
        if status in (401, 403):
            return Failure(KIND_AUTH_FAILED, message)
        if status == 404:
            return Failure(KIND_NOT_FOUND, message)
        if status == 429:
            return Failure(KIND_RATE_LIMITED, message, _retry_after(exc))
        if status >= 500:
            return Failure(KIND_UPSTREAM_ERROR, message)
        return Failure(KIND_FAILED, message)
    if isinstance(exc, (ConnectionError, socket.gaierror)) or names & _NETWORK_NAMES:
        return Failure(KIND_UNREACHABLE, message)
    return Failure(None, message)
