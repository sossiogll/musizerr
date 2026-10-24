"""The order in which the metadata providers are asked: ONE rule, for every reader.

The stored order (``metadata_provider_priority``: the system default, or a user's own) is a
preference, and it can be empty, partial or out of date: a provider is installed after it was
written, another was uninstalled. What every reader needs is the EFFECTIVE order, which these
two pure functions compute from facts the host hands in:

- :func:`in_installation_order`: the providers that exist right now, oldest installation first
  (the stable order a provider the list does not name takes);
- :func:`effective_order`: the stored names that still exist, in their stored order, then every
  other existing provider in the order it was handed in.

The rule knows no provider: it works on names. Reading the stored list, the registry and the
installation order is ``services/provider_order.py``.
"""
from __future__ import annotations

from typing import Iterable


def in_installation_order(available: Iterable[str], installation: Iterable[str]) -> list[str]:
    """``available`` sorted by position in ``installation`` (oldest first).

    A name ``installation`` does not know (a provider with no installation record) goes after
    the known ones, in the order it was given. Duplicates are dropped."""
    seen = list(dict.fromkeys(available))
    position = {name: index for index, name in enumerate(installation)}
    known = sorted((n for n in seen if n in position), key=position.__getitem__)
    return known + [n for n in seen if n not in position]


def effective_order(stored: Iterable[str] | None, available: Iterable[str]) -> list[str]:
    """The order to use: the names of ``stored`` that are in ``available`` (stored order,
    duplicates dropped; a name that no longer exists is skipped), then every name of
    ``available`` that ``stored`` does not name, in the order of ``available``.

    ``available`` is expected in installation order (:func:`in_installation_order`), so a
    provider installed later comes later. An empty or missing ``stored`` gives ``available``."""
    existing = list(dict.fromkeys(available))
    wanted = set(existing)
    kept = [name for name in dict.fromkeys(stored or ()) if name in wanted]
    named = set(kept)
    return kept + [name for name in existing if name not in named]
