"""
Raw TCP from a sandbox: a connection that goes through the gateway's egress proxy.

A sandboxed provider has no network. HTTP libraries find the proxy through the standard proxy
variables, but a library that speaks another protocol over TCP (FTP, whose data connections are on
ports the server picks) opens its own sockets. This module opens them through the same proxy with
``CONNECT host:port``, which the gateway allows only for the destinations the provider's manifest
and the admin approved: a fixed ``egress`` entry, the host a setting named by ``egress_from_settings``
holds, or (any port, or a declared range) the host a setting named by ``tcp_from_settings`` holds.

    from musizerr_sdk import net

    sock = net.create_connection(("files.example.org", 2121), timeout=10)   # a normal socket

``create_connection`` has the signature of :func:`socket.create_connection`, so it can replace it.
The socket it returns is a real ``socket.socket`` whose ``getpeername()`` is the DESTINATION (not the
proxy's loopback), which is what a library that reads the peer's address expects.

Outside a sandbox (no proxy is configured: the provider runs in the host's process, or in a test)
:func:`create_connection` is :func:`socket.create_connection` unchanged. The runner calls
:func:`use_proxy` once when it starts a sandboxed provider; nothing here reads the environment.

**ftplib.** ``ftplib`` calls ``socket.create_connection`` for the control connection and for every
passive data connection, so :func:`route_module` gives the module a view of ``socket`` in which that
one function goes through the proxy::

    import ftplib
    from musizerr_sdk import net

    net.route_module(ftplib)        # once, at import; only ftplib's own use of socket changes

    ftp = ftplib.FTP()
    ftp.connect("files.example.org", 2121, timeout=10)    # control connection: through the proxy
    ftp.login(user, password)
    ftp.retrbinary("RETR song.flac", handle)               # passive data connection: through it too

Passive mode (``ftplib``'s default) is the only one that can work: active mode asks the server to
connect back, and a sandbox accepts nothing. ``FTP`` connects the data connection to the control
socket's peer address, which is the destination, so no extra step is needed.
"""
from __future__ import annotations

import re
import socket
import types
from typing import Any

_proxy: tuple[str, int] | None = None

MAX_HEAD_BYTES = 8 * 1024
MAX_BODY_BYTES = 1024
_HOST = re.compile(r"^[A-Za-z0-9._\-:\[\]]{1,253}$")


class ProxyRefused(PermissionError):
    """The egress proxy refuses the destination: the manifest does not allow it (or no setting names it now)."""


class ProxyUnreachable(ConnectionError):
    """The egress proxy could not open the connection (the destination does not answer, or the proxy failed)."""


def use_proxy(address: tuple[str, int] | None) -> None:
    """Route :func:`create_connection` through the proxy listening at ``address`` (``None``: directly)."""
    global _proxy
    _proxy = address


def proxy_address() -> tuple[str, int] | None:
    return _proxy


class _Tunnel(socket.socket):
    """A connected socket whose peer, as far as the caller is concerned, is the destination."""

    destination: tuple[str, int]

    def getpeername(self) -> Any:  # noqa: D401
        return self.destination


def create_connection(
    address: tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,  # noqa: SLF001 — the default of socket.create_connection
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Like :func:`socket.create_connection`, through the proxy when there is one."""
    proxy = _proxy
    if proxy is None:
        return socket.create_connection(address, timeout, source_address)
    host, port = address
    if source_address is not None:
        raise ValueError("a connection through the egress proxy cannot be bound to a source address")
    if not isinstance(host, str) or not _HOST.match(host) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"{address!r} is not a host and a port")
    shown = f"[{host.strip('[]')}]:{port}" if ":" in host else f"{host}:{port}"
    connection = socket.create_connection(proxy, timeout)
    try:
        connection.sendall(f"CONNECT {shown} HTTP/1.1\r\nHost: {shown}\r\n\r\n".encode("ascii"))
        status, reason = _read_reply(connection)
    except BaseException:
        connection.close()
        raise
    if status != 200:
        connection.close()
        message = f"the egress proxy refused {shown}: {reason}" if status == 403 else f"the egress proxy could not connect to {shown}: {status} {reason}"
        raise (ProxyRefused if status == 403 else ProxyUnreachable)(message)
    kept = connection.gettimeout()
    tunnel = _Tunnel(fileno=connection.detach())
    tunnel.settimeout(kept)
    tunnel.destination = (host, port)
    return tunnel


def _read_reply(connection: socket.socket) -> tuple[int, str]:
    """The proxy's answer to CONNECT: its status and, for a refusal, the reason it gives. Reads the
    head byte by byte, so that whatever the destination sends first (an FTP greeting) stays in the
    socket for the caller."""
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        if len(head) > MAX_HEAD_BYTES:
            raise ProxyUnreachable("the egress proxy's answer is not an HTTP response")
        byte = connection.recv(1)
        if not byte:
            raise ProxyUnreachable("the egress proxy closed the connection without an answer")
        head += byte
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise ProxyUnreachable("the egress proxy's answer is not an HTTP response")
    status = int(parts[1])
    if status == 200:
        return status, ""
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length" and value.strip().isdigit():
            length = min(int(value), MAX_BODY_BYTES)
    body = b""
    while len(body) < length:
        chunk = connection.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return status, body.decode("latin-1").strip() or (parts[2] if len(parts) > 2 else "")


class _SocketModule:
    """What a module sees as ``socket`` once :func:`route_module` ran: the real module, with
    ``create_connection`` replaced. Everything else is forwarded."""

    create_connection = staticmethod(create_connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(socket, name)


def route_module(module: types.ModuleType) -> None:
    """Make ``module``'s own use of ``socket.create_connection`` (``ftplib``) go through the proxy.
    Only that module's reference changes: the rest of the process keeps the real ``socket``."""
    if not isinstance(getattr(module, "socket", None), (types.ModuleType, _SocketModule)):
        raise TypeError(f"{module!r} has no 'socket' module to route")
    module.socket = _SocketModule()  # type: ignore[attr-defined]
