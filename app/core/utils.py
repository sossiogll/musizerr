from __future__ import annotations


def provider_sort_key(name: str, priority_order: list[str]) -> int:
    """Sort key for provider priority ordering."""
    try:
        return priority_order.index(name)
    except ValueError:
        return len(priority_order)
