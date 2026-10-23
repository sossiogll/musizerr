"""
The provider registry of ONE process that runs task code.

A Celery worker is a parent process that forks the children the tasks run in (the default
prefork pool). The parent builds nothing a task needs: a registry it built would be
copied into every child as it was at fork time (the gateway starts its providers a few
seconds after the workers, so usually EMPTY) and the thread that rebuilds it on the
providers-changed event (``services/provider_events.py``) does not survive a fork. So the
registry is built where it is used, lazily, by the process that asks for it: the first
``get()`` of a process builds its own :class:`ProviderRegistry` and starts its own
:class:`ProviderReloader`, and a process whose pid is not the one that built the held registry
(a fork) forgets the inherited one and builds again. The same code serves the solo and
threads pools, which have no children.

It is the only place a worker builds a registry.
"""
from __future__ import annotations

import logging
import os
import threading

from services.provider_events import ProviderReloader
from services.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)


class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._registry: ProviderRegistry | None = None
        self._reloader: ProviderReloader | None = None

    @property
    def reloader(self) -> ProviderReloader | None:
        """The reloader of this process's registry (None until this process built one)."""
        return self._reloader if self._pid == os.getpid() else None

    def get(self) -> ProviderRegistry:
        pid = os.getpid()
        if self._pid == pid and self._registry is not None:
            return self._registry
        with self._lock:
            if self._pid == pid and self._registry is not None:
                return self._registry
            # A registry built before a fork is the parent's: its reloader thread is not running
            # here and its sockets are the parent's. It is dropped, never stopped or closed.
            registry = ProviderRegistry()
            self._reloader = ProviderReloader(registry).start()
            self._registry, self._pid = registry, pid
            logger.info(
                "Provider registry built in process %d: %d metadata, %d download providers",
                pid, len(registry.metadata_names()), len(registry.download_names()),
            )
            return registry
