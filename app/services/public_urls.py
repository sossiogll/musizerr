"""
Prefisso con cui il frontend raggiunge l'API (variabile d'ambiente
``MUSIZEER_PUBLIC_API_PREFIX``, default ``/api``) applicato ai costruttori di
``musizeer_sdk.urls`` e ``core/urls.py``.
"""
from __future__ import annotations


from core import environment, urls
from musizeer_sdk import urls as provider_urls


def api_prefix() -> str:
    return environment.get("MUSIZEER_PUBLIC_API_PREFIX")


def cover_url(provider: str, cover_id: str) -> str:
    return provider_urls.cover_url(api_prefix(), provider, cover_id)


def preview_url(provider: str, item_id: str) -> str:
    return provider_urls.preview_url(api_prefix(), provider, item_id)


def provider_stream_url(provider: str, item_id: str) -> str:
    return provider_urls.provider_stream_url(api_prefix(), provider, item_id)


def stored_stream_url(track_id: str) -> str:
    return urls.stored_stream_url(api_prefix(), track_id)


def duplicate_member_stream_url(member_id: str) -> str:
    return urls.duplicate_member_stream_url(api_prefix(), member_id)


def is_host_served(url: str | None) -> bool:
    """Un URL servito da questo host (anche un riferimento persistito)."""
    return urls.is_host_served(url)


def publish(url: str | None) -> str | None:
    """Un URL letto dal DB, pronto per il client (vedi ``core.urls.publish``)."""
    return urls.publish(api_prefix(), url)


def publish_track_dict(data: dict) -> dict:
    """Una traccia letta dal DB (``tracks.track_data``), pronta per il client:
    unico punto in cui i riferimenti persistiti tornano URL pubblici."""
    return urls.publish_track_dict(api_prefix(), data)
