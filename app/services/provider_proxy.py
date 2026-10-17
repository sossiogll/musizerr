"""
The only object through which the host reaches a provider.

A proxy is built from a provider's manifest (``musizerr_sdk.manifest``) and a
:class:`Transport`. It IS an instance of the abstract classes of the roles the
manifest names, so that the host's type hints and the registry's contracts hold,
but it holds no provider code: the capabilities the provider declares are
answered from the manifest, and every other method of the contract is a call
sent through the transport (``musizerr_sdk.protocol``) to the provider's runner,
with a snapshot of the provider's context, and answered with the decoded result,
the pages of a stream, or an exception:

- the exceptions of the contract (``DownloadDelegated``, ``DownloadDeclined``,
  ``DelegateHoldsRelease``, ``RemotePlaylistGone``, ``ProviderRateLimited``,
  ``PermissionError``, ``NotImplementedError``) are raised as themselves;
- any other failure of the provider is a :class:`ProviderCallFailed`, carrying
  what the runner established about it (a ``kind``, a message, a
  ``retry_after``), which ``services.errors.classify_error`` reads directly;
- a failure of the transport (:class:`TransportError`, which a transport raises when
  the connection is lost or refused) is a ``ProviderCallFailed`` of kind
  ``unreachable``, and a reply that does not follow the protocol one of kind
  ``failed``: a provider that cannot be reached is never a 500.
"""
from __future__ import annotations

import functools
import inspect
import types
from collections.abc import Iterator
from typing import Any, Protocol

from musizerr_sdk import failures, protocol, wire
from musizerr_sdk.context import ContextSnapshot
from musizerr_sdk.interfaces import BaseProvider, StorageStreamResult
from musizerr_sdk.manifest import DECLARED_QUERIES, ROLES, Manifest, declared_names
from musizerr_sdk.protocol import Frame, ProtocolError, RemoteError
from musizerr_sdk.version import peer_minor


class ProviderCallFailed(Exception):
    """A call to a provider failed with something that is not an exception of
    the contract. ``kind`` is the runner's classification (``None`` when the
    type of the provider's exception said nothing: the host then reads the
    message), ``retry_after`` the seconds the upstream asked to wait,
    ``exception_type`` the name of the exception class the provider raised."""

    def __init__(
        self, kind: str | None, message: str, retry_after: float | None = None, *, exception_type: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after
        self.exception_type = exception_type


class TransportError(Exception):
    """The connection to a provider's runner failed (it is gone, refused, broke
    off): what a :class:`Transport` raises, so that a provider's own
    ``ConnectionError`` or ``PermissionError`` is never mistaken for it."""


class Transport(Protocol):
    """Carries a request frame to a provider's runner and brings back the
    frames of the reply, as they are produced. The iterator is closed when the
    caller is done with it."""

    def request(self, request: Frame) -> Iterator[Frame]: ...


class ContextSource(Protocol):
    """The host's side of a provider's ``ProviderContext``: what the next call
    carries."""

    def snapshot(self) -> ContextSnapshot: ...


def _failure(error: RemoteError) -> ProviderCallFailed:
    return ProviderCallFailed(error.kind, error.message, error.retry_after, exception_type=error.type_name)


def _broken(exc: BaseException) -> ProviderCallFailed:
    """What a failure of the transport or of the protocol is for the host."""
    if isinstance(exc, (ProtocolError, wire.WireError)):
        return ProviderCallFailed(failures.KIND_FAILED, f"the provider broke the protocol: {exc}",
                                  exception_type=type(exc).__name__)
    return ProviderCallFailed(failures.KIND_UNREACHABLE, str(exc) or type(exc).__name__,
                              exception_type=type(exc).__name__)


def _guarded(stream: Iterator) -> Iterator:
    """``stream`` (pages or chunks of a reply), with a transport failure at any
    point of the iteration raised as a ``ProviderCallFailed``."""
    try:
        yield from stream
    except (ProtocolError, wire.WireError, TransportError) as exc:
        raise _broken(exc) from exc
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


class ProviderProxy:
    """What every proxy has in common; the contract methods are added to the
    class built for the roles of a manifest (:func:`proxy_class`)."""

    manifest: Manifest

    def __init__(self, manifest: Manifest, transport: Transport, context: ContextSource) -> None:
        self.manifest = manifest
        self.name = manifest.name
        self.label = manifest.label
        self.auth = manifest.auth
        self.roles = frozenset(manifest.roles)
        # The host's own side of the context: where the next call's snapshot comes
        # from (and what the host reads of it, e.g. the AuthManager's timeouts).
        self.context = context
        self._transport = transport
        self._implements = frozenset(manifest.implements)
        # The SDK minor the provider was built on (ADR-0007 §3.1): every call is encoded for it, so a
        # provider built on an older SDK never meets a field it would not know.
        self._peer = peer_minor(manifest.sdk_version)
        attributes, _queries = declared_names(manifest.roles)
        for name in attributes:
            setattr(self, name, manifest.declarations[name])

    def implements(self, method: str) -> bool:
        """Whether the provider implements the optional ``method`` itself (the
        contract's default would answer nothing)."""
        return method in self._implements

    def config_schema(self) -> list:
        return list(self.manifest.config_schema)

    def _invoke(self, method: str, args: tuple, kwargs: dict) -> Any:
        snapshot = None if method in protocol.CONTEXT_FREE_METHODS else self.context.snapshot()
        request = protocol.call_request(method, args, kwargs, snapshot, self._peer)
        try:
            value = protocol.read_reply(self._transport.request(request), _failure)
        except (ProtocolError, wire.WireError, TransportError) as exc:
            raise _broken(exc) from exc
        if isinstance(value, Iterator):
            return _guarded(value)
        if isinstance(value, StorageStreamResult) and value.chunks is not None:
            value.chunks = _guarded(value.chunks)
        return value

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


def _forwarder(name: str, original: Any) -> Any:
    def forward(self, *args, **kwargs):
        return self._invoke(name, args, kwargs)

    forward.__name__ = name
    forward.__doc__ = original.__doc__
    forward.__signature__ = inspect.signature(original)  # type: ignore[attr-defined]
    return forward


def _declared_query(name: str, original: Any) -> Any:
    def declared(self):
        return self.manifest.declarations[name]

    declared.__name__ = name
    declared.__doc__ = original.__doc__
    return declared


@functools.cache
def proxy_class(roles: tuple[str, ...]) -> type:
    """The class of the proxies of a provider that fills ``roles``: a subclass
    of each role's abstract class, every contract method forwarded, the
    declared queries answered from the manifest."""
    bases = tuple(ROLES[role] for role in roles)
    declared_queries = {query for base in (BaseProvider, *bases) for query in DECLARED_QUERIES.get(base, ())}
    namespace: dict[str, Any] = {}
    for base in (BaseProvider, *bases):
        for name, member in vars(base).items():
            if name.startswith("_") or not inspect.isfunction(member) or name in namespace:
                continue
            if name == "config_schema":
                continue
            namespace[name] = (
                _declared_query(name, member) if name in declared_queries else _forwarder(name, member)
            )
    name = "".join(role.title() for role in roles) + "Proxy"
    return types.new_class(name, (ProviderProxy, *bases), exec_body=lambda ns: ns.update(namespace))


def connect(transport: Transport, context: ContextSource) -> ProviderProxy:
    """Ask the provider behind ``transport`` to describe itself and build the
    proxy for it. Raises ``ProviderCallFailed`` when it cannot."""
    try:
        manifest = protocol.read_reply(transport.request(protocol.describe_request()), _failure)
    except (ProtocolError, wire.WireError, TransportError) as exc:
        raise _broken(exc) from exc
    if not isinstance(manifest, Manifest):
        raise ProviderCallFailed(failures.KIND_FAILED, "the provider did not answer with a manifest")
    return proxy_class(manifest.roles)(manifest, transport, context)
