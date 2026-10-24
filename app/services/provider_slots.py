"""
Implementazione host di ``core.provider_slots.ProviderSlots`` su Redis: un
provider con limite N ha N lock ``musizeer:provider-slot:<nome>:<i>``, cosi'
il limite vale fra tutti i worker. Ogni lock scade da solo dopo la durata
massima di un download, quindi un worker morto non blocca il provider.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator, Protocol

import redis

from services.progress import REDIS_URL

_POLL_SECONDS = 0.5


class _Lock(Protocol):
    def acquire(self, blocking: bool = ...) -> bool: ...
    def release(self) -> None: ...


LockFactory = Callable[[str, int], _Lock]


def _redis_lock_factory() -> LockFactory:
    client = redis.Redis.from_url(REDIS_URL)
    return lambda key, lease: client.lock(key, timeout=lease)


class RedisProviderSlots:
    def __init__(
        self,
        lease_seconds: int,
        wait_seconds: int,
        lock_factory: LockFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lease = lease_seconds
        self._wait = wait_seconds
        self._lock = lock_factory or _redis_lock_factory()
        self._clock = clock
        self._sleep = sleep

    @contextmanager
    def acquire(self, provider_name: str, limit: int) -> Iterator[bool]:
        held = self._take_slot(provider_name, limit)
        try:
            yield held is not None
        finally:
            if held is not None:
                try:
                    held.release()
                except redis.exceptions.LockError:
                    pass  # scaduto da solo: è lo scopo della lease

    def _take_slot(self, provider_name: str, limit: int) -> _Lock | None:
        locks = [
            self._lock(f"musizeer:provider-slot:{provider_name}:{i}", self._lease)
            for i in range(limit)
        ]
        deadline = self._clock() + self._wait
        while True:
            for lock in locks:
                if lock.acquire(blocking=False):
                    return lock
            if self._clock() >= deadline:
                return None
            self._sleep(_POLL_SECONDS)
