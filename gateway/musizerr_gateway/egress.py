"""
Egress: the only way out of a sandbox, and what it lets through.

A sandbox has no network (its network namespace is empty). The one thing it can
reach is the Unix socket of ITS provider's proxy, served here: an HTTP proxy that
understands ``CONNECT host:port`` (HTTPS and anything tunnelled) and absolute-form
plain HTTP requests, and refuses every other request. A provider that ignores the
proxy variables does not reach the network slowly: it has no route.

**What a provider may reach** (one :class:`EgressPolicy` per provider):

- its manifest's fixed destinations (``egress``, from the VERIFIED image labels),
  which must resolve to public addresses: a fixed host that suddenly points at a
  private address is a DNS trick, not a feature. A fixed entry may be a wildcard (``*.example.com:443``):
  any SUBDOMAIN of ``example.com`` on that port, never ``example.com`` itself; the name is resolved here
  and a wildcard match is held to the same rule (public addresses only);
- the destinations the CURRENT values of its ``egress_from_settings`` settings name.
  The host (which alone knows the settings) sends them with ``grant``; each grant
  expires, so a host that stops asking withdraws them. A destination the user typed
  may be private (a LAN server, a Tailscale address), but loopback, link-local
  (cloud metadata), unspecified, multicast and reserved addresses are never reachable;
- for a setting of ``tcp_from_settings`` (a protocol that is not HTTP, FTP with its passive data
  connections), the host its value names on ANY port, or the range the manifest declares. The ports
  are the manifest's, the host the setting's (the user typed it: private addresses are allowed).

Everything is decided on the destination the provider names, then the proxy resolves
it itself and connects to the address it checked, so a name cannot change meaning
between the check and the connection. Every refusal is logged and remembered.
"""
from __future__ import annotations

import ipaddress
import logging
import select
import socket
import socketserver
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from musizerr_sdk.failures import EGRESS_HEADER, EGRESS_REFUSED, EGRESS_UNREACHABLE
from musizerr_sdk.permissions import (
    PermissionsError,
    TcpAccess,
    is_wildcard,
    normalize_host,
    parse_egress,
    parse_ports,
    parse_target,
    wildcard_matches,
)

logger = logging.getLogger(__name__)

MAX_HEAD_BYTES = 16 * 1024
MAX_GRANTS = 64
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 600.0
REMEMBERED_REFUSALS = 50

PUBLIC, PRIVATE, NEVER = "public", "private", "never"


def classify(address: str) -> str:
    """``public``; ``private`` (RFC 1918, unique local, the shared 100.64/10 range of
    Tailscale...: reachable only when a setting names it); or ``never``."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return NEVER
    return PUBLIC if ip.is_global else PRIVATE


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    private_ok: bool = False


@dataclass(frozen=True)
class Refusal:
    at: float
    target: str
    reason: str


class EgressPolicy:
    """What ONE provider may reach."""

    def __init__(
        self, provider: str, fixed: Iterable[str], clock: Callable[[], float] = time.monotonic,
        tcp: Iterable[TcpAccess] = (),
    ) -> None:
        self.provider = provider
        parsed = [parse_egress(entry) for entry in fixed]
        self._fixed = frozenset((host, port) for host, port in parsed if not is_wildcard(host))
        self._wildcards = frozenset((host, port) for host, port in parsed if is_wildcard(host))
        # The settings that name a host for raw TCP, and the ports the manifest allows on it.
        self._tcp_ports = {access.setting: parse_ports(access.ports) for access in tcp}
        self._granted: frozenset[tuple[str, int]] = frozenset()
        self._granted_tcp: frozenset[tuple[str, int, int]] = frozenset()
        self._expires = 0.0
        self._clock = clock
        self._lock = threading.Lock()
        self.refused: deque[Refusal] = deque(maxlen=REMEMBERED_REFUSALS)

    # ---- grants ---------------------------------------------------

    def grant(self, targets: Iterable[str], ttl: float, tcp: Mapping[str, Iterable[str]] | None = None) -> int:
        """Replace the destinations the provider's settings name. ``targets`` are ``host:port`` (the
        ``egress_from_settings`` ones); ``tcp`` maps a setting of ``tcp_from_settings`` to the hosts its value
        names (on the ports the manifest declares for it; a setting the manifest does not declare grants
        nothing). Returns how many ``targets`` were understood; the rest are ignored."""
        understood: set[tuple[str, int]] = set()
        for entry in list(targets)[:MAX_GRANTS]:
            try:
                understood.add(parse_target(entry))
            except PermissionsError:
                continue
        raw: set[tuple[str, int, int]] = set()
        for setting, hosts in (tcp or {}).items():
            ports = self._tcp_ports.get(setting)
            if ports is None:
                continue
            for host in list(hosts)[:MAX_GRANTS]:
                try:
                    raw.add((normalize_host(host), *ports))
                except (PermissionsError, AttributeError):
                    continue
        with self._lock:
            self._granted = frozenset(understood)
            self._granted_tcp = frozenset(raw)
            self._expires = self._clock() + max(0.0, ttl)
        return len(understood)

    @property
    def fixed(self) -> frozenset[tuple[str, int]]:
        """The fixed destinations of the manifest, wildcards (``*.example.com``) included."""
        return self._fixed | self._wildcards

    def granted(self) -> frozenset[tuple[str, int]]:
        with self._lock:
            return self._granted if self._clock() < self._expires else frozenset()

    def granted_tcp(self) -> frozenset[tuple[str, int, int]]:
        """``(host, first port, last port)`` the settings of ``tcp_from_settings`` name right now."""
        with self._lock:
            return self._granted_tcp if self._clock() < self._expires else frozenset()

    # ---- decisions ------------------------------------------------

    def decide(self, host: str, port: int) -> Decision:
        try:
            host = normalize_host(host)
        except PermissionsError:
            return Decision(False, "not a valid host name")
        if (host, port) in self.granted():
            return Decision(True, "named by a setting", private_ok=True)
        tcp = self.granted_tcp()
        if any(name == host and first <= port <= last for name, first, last in tcp):
            return Decision(True, "named by a setting (raw TCP)", private_ok=True)
        # Fixed destinations and wildcards must resolve to PUBLIC addresses (private_ok stays False).
        if (host, port) in self._fixed:
            return Decision(True, "declared by the provider")
        if any(p == port and wildcard_matches(pattern, host) for pattern, p in self._wildcards):
            return Decision(True, "declared by the provider (wildcard)")
        known = (
            {h for h, _ in self._fixed} | {h for h, _ in self.granted()} | {h for h, _, _ in tcp}
            | {host for pattern, _ in self._wildcards if wildcard_matches(pattern, host)}
        )
        if host in known:
            return Decision(False, f"port {port} is not allowed for {host}")
        return Decision(False, "not a declared destination")

    def refuse(self, target: str, reason: str) -> None:
        self.refused.append(Refusal(time.time(), target, reason))
        logger.warning("egress refused: provider=%s target=%s reason=%s", self.provider, target, reason)

    def recent_refusals(self) -> list[dict]:
        return [{"at": item.at, "target": item.target, "reason": item.reason} for item in self.refused]


def resolve(host: str, port: int, private_ok: bool) -> list[str]:
    """The addresses of ``host`` the proxy may connect to; raises :class:`PermissionsError`
    when there are none, with the reason."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PermissionsError(f"cannot resolve {host}: {exc.strerror or exc}") from exc
    allowed: list[str] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0].split("%")[0]
        if address in seen:
            continue
        seen.add(address)
        kind = classify(address)
        if kind == PUBLIC or (kind == PRIVATE and private_ok):
            allowed.append(address)
    if not allowed:
        kinds = ", ".join(sorted({f"{a} ({classify(a)})" for a in seen})) or "no address"
        raise PermissionsError(f"{host} resolves only to addresses that may not be reached: {kinds}")
    return allowed


# ------------------------------------------------------------------
# The proxy
# ------------------------------------------------------------------


def splice(left: socket.socket, right: socket.socket, idle_seconds: float = IDLE_TIMEOUT) -> None:
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


def _read_head(connection: socket.socket) -> tuple[bytes, bytes] | None:
    """(the request head up to its blank line, whatever came after it), or None."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEAD_BYTES:
            return None
        chunk = connection.recv(4096)
        if not chunk:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _reply(connection: socket.socket, status: str, body: str = "", verdict: str | None = None) -> None:
    """An answer the proxy gives itself. ``verdict`` (``EGRESS_REFUSED`` or
    ``EGRESS_UNREACHABLE``) goes in ``EGRESS_HEADER``, so the provider's side
    tells it from an answer of the destination (``musizerr_sdk.failures``)."""
    payload = body.encode()
    marker = f"{EGRESS_HEADER}: {verdict}\r\n" if verdict else ""
    try:
        connection.sendall(
            f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\n{marker}Connection: close\r\n\r\n".encode() + payload,
        )
    except OSError:
        pass


def _split_authority(authority: str, default_port: int | None) -> tuple[str, int] | None:
    host, separator, port = authority.rpartition(":")
    if separator and (":" not in host or host.endswith("]")) and port.isdigit():
        return host, int(port)
    if default_port is None or not authority or (":" in authority and not authority.endswith("]")):
        return None
    return authority, default_port


class _Handler(socketserver.BaseRequestHandler):
    server: "EgressServer"

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.settimeout(30)
        read = _read_head(connection)
        if read is None:
            return _reply(connection, "400 Bad Request", "bad request")
        head, rest = read
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError):
            return _reply(connection, "400 Bad Request", "bad request line")
        policy = self.server.policy
        if method.upper() == "CONNECT":
            destination = _split_authority(target, None)
            if destination is None:
                policy.refuse(target, "malformed CONNECT target")
                return _reply(connection, "400 Bad Request", "CONNECT needs host:port")
            upstream = self._open(destination, target)
            if upstream is None:
                return None
            try:
                connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                connection.settimeout(None)
                splice(connection, upstream)
            finally:
                upstream.close()
            return None
        # Plain HTTP in absolute form: http://host[:port]/path
        if not target.lower().startswith("http://"):
            policy.refuse(target, "a proxy request must be CONNECT or an absolute http:// URL")
            return _reply(connection, "400 Bad Request", "only CONNECT and http:// URLs")
        authority, slash, path = target[len("http://"):].partition("/")
        destination = _split_authority(authority, 80)
        if destination is None:
            policy.refuse(target, "malformed URL")
            return _reply(connection, "400 Bad Request", "malformed URL")
        upstream = self._open(destination, f"{authority}")
        if upstream is None:
            return None
        try:
            kept = [
                line for line in lines[1:]
                if line.split(b":", 1)[0].strip().lower() not in (b"proxy-connection", b"proxy-authorization", b"connection")
            ]
            origin = [f"{method} /{path} {version}".encode(), *kept, b"Connection: close"]
            upstream.sendall(b"\r\n".join(origin) + b"\r\n\r\n" + rest)
            connection.settimeout(None)
            splice(connection, upstream)
        finally:
            upstream.close()
        return None

    def _open(self, destination: tuple[str, int], shown: str) -> socket.socket | None:
        host, port = destination
        policy = self.server.policy
        connection: socket.socket = self.request
        decision = policy.decide(host, port)
        if not decision.allowed:
            policy.refuse(f"{host}:{port}", decision.reason)
            _reply(connection, "403 Forbidden", f"egress to {host}:{port} is not allowed for this provider", EGRESS_REFUSED)
            return None
        try:
            addresses = self.server.resolver(normalize_host(host), port, decision.private_ok)
        except PermissionsError as exc:
            policy.refuse(f"{host}:{port}", str(exc))
            _reply(connection, "403 Forbidden", str(exc), EGRESS_REFUSED)
            return None
        last: OSError | None = None
        for address in addresses:
            try:
                return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT)
            except OSError as exc:
                last = exc
        logger.info("egress: provider=%s %s:%s unreachable: %s", policy.provider, host, port, last)
        _reply(connection, "502 Bad Gateway", f"cannot connect to {host}:{port}", EGRESS_UNREACHABLE)
        return None


class EgressServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The proxy of one provider, on a Unix socket."""

    daemon_threads = True

    def __init__(
        self, socket_path: Path, policy: EgressPolicy,
        resolver: Callable[[str, int, bool], list[str]] = resolve,
    ) -> None:
        self.policy = policy
        self.resolver = resolver
        self.socket_path = socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        super().__init__(str(socket_path), _Handler)
        socket_path.chmod(0o600)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)
