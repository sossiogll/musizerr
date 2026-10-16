"""
Cool-down of a download provider that keeps failing transiently (network,
5xx, timeouts). The resolver asks before calling a provider and reports how
each call went; the host decides how to count failures across processes and
for how long a provider rests (``services.provider_cooldown``).
"""
from __future__ import annotations

from typing import Protocol


class ProviderCooldown(Protocol):
    def cooling(self, provider_name: str) -> bool:
        """True while ``provider_name`` rests after too many transient errors."""
        ...

    def remaining_seconds(self, provider_name: str) -> float:
        """How long ``provider_name`` still rests (0 when it does not)."""
        ...

    def record(self, provider_name: str, ok: bool) -> None:
        """How a call went: ``ok`` is any answer (a file, or "not found"),
        not ok a transient error."""
        ...


class NoCooldown:
    """No cool-down: every provider is always tried."""

    def cooling(self, provider_name: str) -> bool:
        return False

    def remaining_seconds(self, provider_name: str) -> float:
        return 0.0

    def record(self, provider_name: str, ok: bool) -> None:
        return None
