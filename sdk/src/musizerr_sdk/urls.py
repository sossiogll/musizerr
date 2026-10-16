"""
The public URLs the host serves for a provider's covers, full streams and
previews. The frontend reaches the API only under a prefix (``/api`` behind
Vite or nginx): a path relative to the root would load the frontend page
instead of the resource. ``ProviderContext.cover_url`` / ``stream_url`` /
``preview_url`` build them here, with the prefix the host gives.
"""
from __future__ import annotations

from urllib.parse import quote


def join_path(prefix: str, *segments: str) -> str:
    base = "/" + prefix.strip("/") if prefix.strip("/") else ""
    return base + "/" + "/".join(quote(str(s), safe="") for s in segments)


def cover_url(prefix: str, provider: str, cover_id: str) -> str:
    """Cover che il backend inoltra dal provider (``GET /cover/provider/...``)."""
    return join_path(prefix, "cover", "provider", provider, cover_id)


def provider_stream_url(prefix: str, provider: str, item_id: str) -> str:
    """Audio completo servito da un provider (``GET /stream/provider/...``)."""
    return join_path(prefix, "stream", "provider", provider, item_id)


def preview_url(prefix: str, provider: str, item_id: str) -> str:
    """Anteprima di un provider risolta a ogni richiesta (``GET /preview/...``):
    per le anteprime il cui URL firmato scade."""
    return join_path(prefix, "preview", provider, item_id)
