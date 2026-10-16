"""
The runner: the provider's side of the call protocol (``musizerr_sdk.protocol``),
standard library only.

A runner holds ONE provider, built by the runner itself from the provider's
class (or any ``factory(context)``) the first time it is asked for something,
and answers the host's requests: ``describe`` gives the manifest, ``call``
calls a method of the contract and sends back the result, the pages of a
stream, or the error. Whatever transport carries the frames (the loopback of
the tests, a Unix socket under ``python -m musizerr_sdk.runner``
in a sandbox) the runner is the same: it sees decoded messages and returns frames.

The provider's ``ProviderContext`` is built from the snapshot each call
carries. It is bound to the thread that runs the call, so concurrent calls
(a search over many providers) each see their own settings, and a stream sees
the snapshot of the call that opened it at every step.
"""
from __future__ import annotations

import argparse
import importlib
import os
import select
import signal
import socket
import socketserver
import sys
import threading
from collections.abc import Iterator as IteratorABC
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from . import net, urls, wire
from .context import ContextSnapshot, ProviderContext
from .interfaces import BaseProvider, StorageStreamResult
from .manifest import build, contract_methods
from .protocol import (
    CONTEXT_FREE_METHODS,
    OP_DESCRIBE,
    Frame,
    FrameSocket,
    ProtocolError,
    bytes_frame,
    end_frame,
    error_frame,
    item_frame,
    join_session,
    manifest_frame,
    parse_request,
    result_frame,
    stream_frame,
)

ProviderFactory = Callable[[ProviderContext], BaseProvider]


class SnapshotContext:
    """A ``ProviderContext`` answering from the snapshot bound to the calling
    thread (``bound``); with none bound, from the defaults."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._default = ContextSnapshot()
        self.provider_name = "provider"

    @contextmanager
    def bound(self, snapshot: ContextSnapshot | None) -> Iterator[None]:
        previous = getattr(self._local, "snapshot", None)
        self._local.snapshot = snapshot
        try:
            yield
        finally:
            self._local.snapshot = previous

    def _current(self) -> ContextSnapshot:
        return getattr(self._local, "snapshot", None) or self._default

    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(self._current().settings)

    def setting(self, key: str, default: Any = None) -> Any:
        return self._current().settings.get(key, default)

    @property
    def http_timeout(self) -> int:
        return self._current().http_timeout

    @property
    def download_timeout(self) -> int:
        return self._current().download_timeout

    @property
    def connect_timeout(self) -> int:
        return self._current().connect_timeout

    @property
    def stream_start_timeout(self) -> int:
        return self._current().stream_start_timeout

    def cover_url(self, cover_id: str) -> str:
        return urls.cover_url(self._current().api_prefix, self.provider_name, cover_id)

    def stream_url(self, item_id: str) -> str:
        return urls.provider_stream_url(self._current().api_prefix, self.provider_name, item_id)

    def preview_url(self, item_id: str) -> str:
        return urls.preview_url(self._current().api_prefix, self.provider_name, item_id)


class ProviderRunner:
    """Answers the requests of a host for one provider.

    ``factory`` is the provider's class, or any callable taking the provider's
    ``ProviderContext`` and returning the provider."""

    def __init__(self, factory: ProviderFactory) -> None:
        self._factory = factory
        self._context = SnapshotContext()
        self._provider: BaseProvider | None = None
        self._methods: frozenset[str] = frozenset()
        self._lock = threading.Lock()

    def _provider_instance(self) -> BaseProvider:
        provider = self._provider
        if provider is None:
            with self._lock:
                if self._provider is None:
                    built = self._factory(self._context)
                    self._context.provider_name = getattr(built, "name", self._context.provider_name)
                    self._methods = contract_methods(built)
                    self._provider = built
                provider = self._provider
        return provider

    # ------------------------------------------------------------------

    def handle(self, request: Frame) -> Iterator[Frame]:
        """The frames that answer ``request``, produced as they are consumed."""
        try:
            message = parse_request(request)
            snapshot = wire.decode(message.get("context"))
            if snapshot is not None and not isinstance(snapshot, ContextSnapshot):
                raise ProtocolError("'context' is not a ContextSnapshot")
        except Exception as exc:  # noqa: BLE001 — whatever it is, it is the host's answer
            yield error_frame(exc)
            return
        if message["op"] == OP_DESCRIBE:
            yield self._describe(snapshot)
        else:
            yield from self._call(message, snapshot)

    def _describe(self, snapshot: ContextSnapshot | None) -> Frame:
        try:
            provider = self._provider_instance()
            with self._context.bound(snapshot):
                return manifest_frame(build(provider))
        except Exception as exc:  # noqa: BLE001
            return error_frame(exc)

    def _call(self, message: dict, snapshot: ContextSnapshot | None) -> Iterator[Frame]:
        try:
            provider = self._provider_instance()
            method = message["method"]
            if method not in self._methods:
                raise ProtocolError(f"'{method}' is not a method of the contract")
            args, kwargs = join_session(
                wire.decode(message["args"]), wire.decode(message["kwargs"]),
                wire.decode(message["session"]), message.get("session_at"),
            )
            if method in CONTEXT_FREE_METHODS:
                snapshot = None
            with self._context.bound(snapshot):
                result = getattr(provider, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — a provider can raise anything
            yield error_frame(exc)
            return
        if isinstance(result, IteratorABC):
            yield stream_frame()
            yield from self._pages(result, snapshot)
            return
        chunks = result.chunks if isinstance(result, StorageStreamResult) else None
        try:
            reply = result_frame(result, chunks=chunks is not None)
        except Exception as exc:  # noqa: BLE001
            yield error_frame(exc)
            return
        yield reply
        if chunks is not None:
            yield from self._bytes(chunks, snapshot)

    def _steps(self, iterator: Iterator, snapshot: ContextSnapshot | None, frame: Callable[[Any], Frame]) -> Iterator[Frame]:
        """One frame per item of ``iterator``, each produced on demand, then the
        end of the stream (or the error that stopped it)."""
        iterator = iter(iterator)
        try:
            while True:
                try:
                    with self._context.bound(snapshot):
                        item = next(iterator)
                except StopIteration:
                    break
                yield frame(item)
            yield end_frame()
        except Exception as exc:  # noqa: BLE001 — raised by the provider or by encoding an item
            yield error_frame(exc)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                with self._context.bound(snapshot):
                    close()

    def _pages(self, iterator: Iterator, snapshot: ContextSnapshot | None) -> Iterator[Frame]:
        return self._steps(iterator, snapshot, item_frame)

    def _bytes(self, chunks: Iterator[bytes], snapshot: ContextSnapshot | None) -> Iterator[Frame]:
        return self._steps(chunks, snapshot, bytes_frame)


# ------------------------------------------------------------------
# Unix socket
# ------------------------------------------------------------------


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        runner: ProviderRunner = self.server.runner  # type: ignore[attr-defined]
        frames = FrameSocket(self.request)
        while True:
            try:
                request = frames.receive()
            except (ProtocolError, OSError):
                return
            if request is None:
                return
            replies = runner.handle(request)
            try:
                for reply in replies:
                    frames.send(reply)
            except OSError:
                return
            finally:
                replies.close()


class RunnerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Serves a runner on a Unix socket, a thread per connection, any number of
    requests in turn on each. The socket is readable by its owner only."""

    daemon_threads = True

    def __init__(self, socket_path: str, runner: ProviderRunner) -> None:
        self.runner = runner
        self.socket_path = socket_path
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        super().__init__(socket_path, _Handler)
        os.chmod(socket_path, 0o600)

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass



# ------------------------------------------------------------------
# The way out of a sandbox
# ------------------------------------------------------------------
#
# A sandboxed provider has no network. Its only way out is the gateway's egress
# proxy, reachable through a Unix socket bound into the sandbox. Libraries speak
# TCP to a proxy, so the runner listens on the loopback of the sandbox and
# forwards every connection to that socket, and points the standard proxy
# variables at it. The decision of what may pass is the gateway's, never the
# provider's: a provider that ignores the variables has no route at all.

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _splice(left: socket.socket, right: socket.socket, idle_seconds: float = 600.0) -> None:
    """Copy bytes both ways between two connected sockets until either side ends."""
    sockets = (left, right)
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], idle_seconds)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)
    except OSError:
        return


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(10)
            upstream.connect(self.server.target)  # type: ignore[attr-defined]
            upstream.settimeout(None)
            _splice(self.request, upstream)
        except OSError:
            pass
        finally:
            upstream.close()


class EgressRelay(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A TCP listener on the loopback that forwards every connection to the Unix
    socket ``target`` (the gateway's egress proxy)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(("127.0.0.1", 0), _RelayHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def route_through(relay: EgressRelay, environ: dict[str, str] | None = None) -> None:
    """Point the proxy variables of the process at ``relay`` (and nothing is exempt)."""
    environ = os.environ if environ is None else environ
    for name in PROXY_VARIABLES:
        environ[name] = relay.url
    environ["NO_PROXY"] = environ["no_proxy"] = ""


# ------------------------------------------------------------------
# Command line: ``python -m musizerr_sdk.runner``
# ------------------------------------------------------------------


def load_provider_class(spec: str) -> ProviderFactory:
    """``"package.module:ClassName"`` as the class."""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"--provider expects 'module:Class', got '{spec}'")
    return getattr(importlib.import_module(module_name), attribute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m musizerr_sdk.runner", description="Serve one provider on a Unix socket.")
    parser.add_argument("--provider", required=True, help="the provider class, 'module:Class'")
    parser.add_argument("--socket", help="the Unix socket to serve the call protocol on")
    parser.add_argument("--egress-socket", help="the gateway's egress proxy (a Unix socket) to reach the network through")
    parser.add_argument("--print-labels", action="store_true", help="print the image labels of the provider (key=value lines) and exit")
    args = parser.parse_args(argv)

    if args.print_labels:
        from .context import StaticProviderContext
        from .labels import labels_for

        provider_class = load_provider_class(args.provider)
        provider = provider_class(StaticProviderContext(provider_name=getattr(provider_class, "name", "provider")))
        for key, value in labels_for(build(provider)).items():
            print(f"{key}={value}")
        return 0

    if not args.socket:
        parser.error("--socket is required")
    relay: EgressRelay | None = None
    if args.egress_socket:
        relay = EgressRelay(args.egress_socket)
        threading.Thread(target=relay.serve_forever, name="egress-relay", daemon=True).start()
        route_through(relay)
        net.use_proxy(relay.server_address)  # raw TCP (FTP...) goes the same way: musizerr_sdk.net
    server = RunnerServer(args.socket, ProviderRunner(load_provider_class(args.provider)))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if relay is not None:
            relay.shutdown()
            relay.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
