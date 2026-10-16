"""
Errori mostrati a chi non è admin. Un'eccezione grezza (es. "[Errno -3]
Temporary failure in name resolution") descrive l'infrastruttura, non il
problema per l'utente: fuori dal ruolo admin si mostrano solo un codice
stabile e un messaggio leggibile; il testo grezzo resta in ``error_detail``
per gli admin.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from core import download_states as ds
from core.auth_manager import AuthError, ProviderNotConfigured
from musizerr_sdk.failures import classify_exception
from core.provider_failures import ProviderFailure
from services.log_redaction import redact
from services.provider_proxy import ProviderCallFailed

# Classificazione generica del testo grezzo: il primo gruppo che combacia.
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # A download that never reached a worker (``db.download_store.mark_unqueued``):
    # first, its text carries the broker's own error, which would classify as
    # something else.
    (ds.QUEUE_UNAVAILABLE, (ds.QUEUE_UNAVAILABLE,)),
    # ``musizerr_sdk.errors.DownloadDeclined``: the provider answered that
    # it cannot provide this track, for a stated reason (not "not found").
    ("declined", ("declined (",)),
    ("not_found", ("no provider could download",)),
    ("timeout", ("timed out", "timeout")),
    ("unreachable", (
        "name resolution", "name or service not known", "nodename nor servname",
        "connection refused", "connecterror", "connection error", "no route to host",
        "network is unreachable", "unreachable", "errno",
    )),
    ("auth_failed", (
        "401", "403", "unauthorized", "forbidden", "invalid api key",
        "invalid credentials", "authentication", "not authorized",
    )),
    ("rate_limited", ("429", "too many requests", "rate limit")),
    ("upstream_error", (
        "500", "502", "503", "internal server error", "bad gateway",
        "service unavailable", "server error",
    )),
)

_MESSAGES = {
    "declined": "cannot provide this track (e.g. it already has the release, without this track).",
    "not_found": "had nothing matching this track.",
    "timeout": "took too long to answer.",
    "unreachable": "could not be reached.",
    "auth_failed": "refused the credentials.",
    "rate_limited": "is limiting requests, try again shortly.",
    "upstream_error": "answered with an error.",
    "not_configured": "is not configured.",
    "failed": "failed.",
}

# Kinds that are not about a provider: the code is the kind itself (no subject
# prefix) and the message stands alone.
_OWN_MESSAGES = {
    ds.QUEUE_UNAVAILABLE: "The download could not be queued: it will be tried again automatically.",
}

_SUBJECTS = {
    "sync_provider": "The sync provider",
    "metadata_provider": "The provider",
    "provider": "The provider",
    "download_provider": "The download provider",
    "routine": "The routine",
    "lyrics_provider": "The lyrics provider",
}


def _pattern(needles: tuple[str, ...]) -> re.Pattern[str]:
    """Un codice numerico (es. "401") vale solo come numero intero, mai
    dentro un altro numero o un id; il resto è una sottostringa."""
    return re.compile("|".join(
        rf"(?<!\d){re.escape(needle)}(?!\d)" if needle.isdigit() else re.escape(needle)
        for needle in needles
    ))


_PATTERNS = tuple((kind, _pattern(needles)) for kind, needles in _KINDS)


def classify(raw: str) -> str:
    text = raw.lower()
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    return "failed"


def classify_error(error: BaseException | str) -> str:
    """Come :func:`classify`, ma legge prima il tipo dell'eccezione quando lo
    riconosce: il testo resta il ripiego per ciò che non ha un tipo noto.

    Un fallimento di un provider arriva già classificato: il runner ne ha visto
    l'eccezione vera e ne ha stabilito il ``kind`` (``ProviderCallFailed``; se il
    tipo non diceva nulla, resta il testo). Per le eccezioni che non vengono da
    un provider (l'host stesso, l'SDK) vale lo stesso classificatore del
    runner, ``musizerr_sdk.failures``."""
    if isinstance(error, str):
        return classify(error)
    if isinstance(error, ProviderNotConfigured):
        return "not_configured"
    if isinstance(error, ProviderCallFailed):
        return error.kind or classify(error.message)
    failure = classify_exception(error)
    if failure.kind is not None:
        return failure.kind
    if isinstance(error, AuthError):
        return "auth_failed"
    return classify(failure.message)


# Stato HTTP di una rotta di dettaglio quando il suo provider fallisce:
# 404 se il provider dice che la risorsa non c'è, 503 (riprovabile) se il
# provider è irraggiungibile, lento, limitato o non configurato, 502 per ogni
# altra risposta sbagliata del provider. Mai 500: non è un errore dell'host.
_HTTP_STATUS = {
    "not_found": 404,
    "unreachable": 503,
    "timeout": 503,
    "rate_limited": 503,
    "not_configured": 503,
}


def http_status_for(kind: str) -> int:
    return _HTTP_STATUS.get(kind, 502)


def provider_errors(failures: Iterable[ProviderFailure], subject: str = "metadata_provider") -> list[dict[str, Any]]:
    """``[{provider, error_code, error_message}]`` dei provider che hanno
    fallito in un'operazione aggregata. Mai il testo grezzo: sono rotte che
    anche un ospite può chiamare, e il dettaglio resta nel log."""
    out = []
    for failure in failures:
        body = public_error(failure.error, failure.subject or subject, show_detail=False)
        out.append({"provider": failure.provider, **body})
    return out


def public_error(raw: BaseException | str, subject: str, *, show_detail: bool) -> dict[str, Any]:
    """``{"error_code", "error_message"}`` per ``raw`` (un testo o
    un'eccezione), più ``error_detail`` (il testo grezzo) se ``show_detail``."""
    kind = classify_error(raw)
    if kind in _OWN_MESSAGES:
        body: dict[str, Any] = {"error_code": kind, "error_message": _OWN_MESSAGES[kind]}
    else:
        body = {
            "error_code": f"{subject}_{kind}",
            "error_message": f"{_SUBJECTS[subject]} {_MESSAGES[kind]}",
        }
    if show_detail:
        # Anche per un admin: il testo grezzo può contenere un URL con le
        # credenziali del provider.
        body["error_detail"] = redact(raw if isinstance(raw, str) else (str(raw) or type(raw).__name__))
    return body
