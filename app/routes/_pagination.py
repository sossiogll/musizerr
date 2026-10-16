from __future__ import annotations

from typing import Any


def paginated_response(
    items: list[Any],
    *,
    total: int | None = None,
    page: int = 1,
    limit: int | None = None,
) -> dict:
    n = total if total is not None else len(items)
    lim = limit if limit is not None else n
    pages = max(1, (n + lim - 1) // lim) if lim > 0 else 1
    return {
        "items": items,
        "total": n,
        "page": page,
        "limit": lim,
        "pages": pages,
    }
