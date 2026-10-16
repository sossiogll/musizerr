"""
How the tests give a provider to the host: only through the call protocol.

No provider lives in this repository, and in production the providers run in
the gateway's sandboxes, reached over a socket. The tests reach a provider they
build themselves the same way, with the runner in this process instead of the
sandbox: the LOOPBACK transport. Nothing is passed by reference. A request
becomes the bytes of a frame, the runner decodes them, calls the provider, and
encodes its answer into frames that are read back from their bytes: the
manifest, the codec, the framing and the runner's dispatch are all in the path
of every call. Only the socket is missing.

A test builds its provider (a fake) and keeps it: that is the PROVIDER side,
where it asserts what the provider was asked. What it hands to the host (a
registry, a function under test) is ``as_proxy(provider)``: a proxy built from
the provider's manifest whose every call goes through the codec, the framing and
a runner's dispatch before it reaches the instance. The host never holds the
instance (``tests/host_boundary.py`` fails a test in which it does).

``host_proxy(cls)`` is the other way round, for a provider the RUNNER builds from
its class with the context snapshot the host sends, as it will in production.
"""
from __future__ import annotations

import io
import weakref
from collections.abc import Iterator
from typing import Any

from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.protocol import Frame, pack_frame, read_frame
from musizerr_sdk.runner import ProviderFactory, ProviderRunner
from services import provider_proxy
from services.provider_context import HostProviderContext
from services.provider_proxy import ContextSource, ProviderProxy


def _over_the_wire(frame: Frame) -> Frame:
    """``frame`` as it comes out of a connection after being sent over it."""
    received = read_frame(io.BytesIO(pack_frame(frame)).read)
    assert received is not None
    return received


class LoopbackTransport:
    def __init__(self, runner: ProviderRunner) -> None:
        self._runner = runner

    def request(self, request: Frame) -> Iterator[Frame]:
        replies = self._runner.handle(_over_the_wire(request))
        try:
            for reply in replies:
                yield _over_the_wire(reply)
        finally:
            replies.close()


def loopback_proxy(factory: ProviderFactory, context: ContextSource) -> ProviderProxy:
    """The proxy of the provider ``factory`` (its class, or a callable building
    it from its ``ProviderContext``) runs behind a runner in this process."""
    return provider_proxy.connect(LoopbackTransport(ProviderRunner(factory)), context)


_proxies: "weakref.WeakKeyDictionary[object, ProviderProxy]" = weakref.WeakKeyDictionary()


def as_proxy(provider) -> ProviderProxy:
    """The host's view of ``provider``; always the same proxy for the same provider."""
    if isinstance(provider, ProviderProxy):
        return provider
    proxy = _proxies.get(provider)
    if proxy is None:
        context = getattr(provider, "context", None)
        if not isinstance(context, StaticProviderContext):
            context = StaticProviderContext(provider_name=provider.name)
        proxy = loopback_proxy(lambda _context, built=provider: built, context)
        _proxies[provider] = proxy
    return proxy


class HostContextWithoutDatabase(HostProviderContext):
    """The host's real context (the timeouts of the configuration, the public URL
    prefix of the environment), with the provider's settings given by the test
    instead of read from the settings table."""

    def __init__(self, provider_name: str, settings: dict[str, Any] | None = None) -> None:
        super().__init__(provider_name)
        self._given = dict(settings or {})

    def settings(self) -> dict[str, Any]:
        return dict(self._given)


def host_proxy(provider_class, settings: dict[str, Any] | None = None) -> ProviderProxy:
    """The proxy of ``provider_class`` built BY THE RUNNER, with the snapshot of
    the host's context at every call."""
    return loopback_proxy(provider_class, HostContextWithoutDatabase(provider_class.name, settings))
