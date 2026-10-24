"""
Host implementation of ``core.provider_cooldown.ProviderCooldown`` on Redis,
so the count holds across workers: consecutive transient errors of a
provider in ``musizeer:provider-errors:<name>``; past ``errors`` of them the
provider rests for ``seconds`` (``musizeer:provider-cooldown:<name>``, which
expires by itself). Any answer resets the count. ``errors = 0``: off.

A heuristic, not a lock: two workers failing at the same instant may count
one error less, which only delays the rest by one error.

``scope`` keeps two uses apart: the download resolver counts the errors of
download providers (``provider``), the stream route those of the storage
while someone listens (``storage-stream``).
"""
from __future__ import annotations

import logging

import redis

from services.progress import REDIS_URL

logger = logging.getLogger(__name__)


_shared_client = None


def _client():
    """One Redis client per process (it pools its connections): the stream
    route builds a cool-down per request."""
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis.from_url(REDIS_URL)
    return _shared_client


class RedisProviderCooldown:
    def __init__(self, errors: int, seconds: int, client=None, *, scope: str = "provider") -> None:
        self._errors = errors
        self._seconds = seconds
        self._scope = scope
        self._client = client if client is not None else _client()

    def _count_key(self, name: str) -> str:
        return f"musizeer:{self._scope}-errors:{name}"

    def _rest_key(self, name: str) -> str:
        return f"musizeer:{self._scope}-cooldown:{name}"

    def cooling(self, provider_name: str) -> bool:
        if self._errors <= 0:
            return False
        try:
            return bool(self._client.exists(self._rest_key(provider_name)))
        except Exception as exc:  # noqa: BLE001 — without Redis nobody rests
            logger.debug("cool-down check failed for %s: %s", provider_name, exc)
            return False

    def remaining_seconds(self, provider_name: str) -> float:
        if self._errors <= 0:
            return 0.0
        try:
            ttl = self._client.ttl(self._rest_key(provider_name))
        except Exception as exc:  # noqa: BLE001 — see cooling()
            logger.debug("cool-down ttl failed for %s: %s", provider_name, exc)
            return 0.0
        return float(ttl) if ttl and ttl > 0 else 0.0

    def record(self, provider_name: str, ok: bool) -> None:
        if self._errors <= 0:
            return
        key = self._count_key(provider_name)
        try:
            if ok:
                self._client.delete(key)
                return
            count = int(self._client.get(key) or 0) + 1
            if count >= self._errors:
                logger.warning("%s failed %d times in a row: resting %ds", provider_name, count, self._seconds)
                self._client.set(self._rest_key(provider_name), "1", ex=self._seconds)
                self._client.delete(key)
            else:
                self._client.set(key, str(count), ex=self._seconds)
        except Exception as exc:  # noqa: BLE001 — see cooling()
            logger.debug("cool-down record failed for %s: %s", provider_name, exc)
