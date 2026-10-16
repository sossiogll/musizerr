"""
The host never holds a provider.

Every provider reaches the host as a proxy (``services/provider_proxy.py``);
the instance lives behind a runner. A test that hands the host a raw provider
(a fake, or a real provider with a ``StaticProviderContext``) instead of
``tests.loopback.as_proxy(provider)`` would test the host against a provider
object it can never have, and skip the call protocol. This makes that an
error: reading a public attribute of a provider instance from host code
(``core``, ``services``, ``routes``, ``workers``, ``db``) fails the test.

Reads from the provider's own code, from the runner and from the tests (the
provider side of the boundary: what they set up and assert) are free.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

import pytest

from musizerr_sdk.interfaces import BaseProvider

HOST_PACKAGES = ("core.", "services.", "routes.", "workers.", "db.")


_suspended = False


@contextmanager
def suspended():
    """For the one thing that compares the protocol with a host holding the provider
    (the benchmark of the loopback)."""
    global _suspended
    _suspended = True
    try:
        yield
    finally:
        _suspended = False


def _is_proxy(provider: object) -> bool:
    return any(cls.__name__ == "ProviderProxy" for cls in type(provider).__mro__)


def install() -> None:
    if "__getattribute__" in vars(BaseProvider):
        return
    original = object.__getattribute__

    def guarded(self, name):
        if not name.startswith("_") and not _suspended and not _is_proxy(self):
            module = sys._getframe(1).f_globals.get("__name__", "")
            if module.startswith(HOST_PACKAGES):
                pytest.fail(
                    f"{module} read '{name}' of a provider instance ({type(self).__name__}): "
                    "give the host tests.loopback.as_proxy(provider), never the provider itself",
                    pytrace=False,
                )
        return original(self, name)

    BaseProvider.__getattribute__ = guarded
