"""
Exponential backoff with jitter, for everything the host retries later (a
download after a transient failure, a like waiting for the sync provider to
index a track). Pure: the caller passes its own base, cap and random number.
"""

from __future__ import annotations

# Largest fraction taken off each wait at random: attempts born together (a
# whole album) spread out instead of hitting the upstream at once, and a
# wait never exceeds the cap.
RETRY_JITTER = 0.2


def retry_delay(attempt: int, base_seconds: float, jitter: float, max_seconds: float) -> float:
    """
    Wait before the attempt after the ``attempt``-th (1 = first retry):
    ``base_seconds`` doubled at each attempt up to ``max_seconds``, minus up
    to ``RETRY_JITTER`` of itself in proportion to ``jitter`` (a random
    number in [0, 1)).
    """
    doublings = min(max(0, attempt - 1), 32)
    delay = min(max_seconds, base_seconds * 2 ** doublings)
    return delay * (1 - RETRY_JITTER * min(max(jitter, 0.0), 1.0))
