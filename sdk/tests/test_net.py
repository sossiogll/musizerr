"""Raw TCP through the egress proxy: the helper a sandboxed provider opens sockets with, and ftplib on top of it."""
from __future__ import annotations

import ftplib
import socket
import threading

import pytest

from musizeer_sdk import net


def serve(handler):
    """A TCP server on the loopback; ``handler(connection)`` runs in a thread per connection."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def loop():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=run, args=(connection,), daemon=True).start()

    def run(connection):
        with connection:
            try:
                handler(connection)
            except OSError:
                pass

    threading.Thread(target=loop, daemon=True).start()
    return listener


def read_head(connection) -> bytes:
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            break
        data += chunk
    return data


class Proxy:
    """A CONNECT proxy: ``routes`` maps "host:port" to the real (host, port); anything else is refused (403) or,
    for a name in ``dead``, a bad gateway. It records what was asked."""

    def __init__(self, routes=None, dead=(), hosts=None):
        # ``hosts`` maps a name to an address reachable on ANY port (what a tcp_from_settings grant allows).
        self.routes, self.dead, self.hosts, self.asked = dict(routes or {}), set(dead), dict(hosts or {}), []
        self.listener = serve(self.handle)

    @property
    def address(self):
        return self.listener.getsockname()

    def handle(self, connection):
        head = read_head(connection).decode()
        target = head.split(" ")[1]
        self.asked.append(target)
        if target in self.dead:
            connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        name, _, port = target.rpartition(":")
        real = self.routes.get(target) or ((self.hosts[name], int(port)) if name in self.hosts else None)
        if real is None:
            body = f"egress to {target} is not allowed for this provider".encode()
            connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            return
        with socket.create_connection(real) as upstream:
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            splice(connection, upstream)


def splice(left, right):
    import select

    while True:
        readable, _, _ = select.select([left, right], [], [], 5)
        if not readable:
            return
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


@pytest.fixture(autouse=True)
def direct():
    yield
    net.use_proxy(None)


# ------------------------------------------------------------------
# The connection
# ------------------------------------------------------------------


def test_without_a_proxy_it_is_the_ordinary_connection():
    listener = serve(lambda c: c.sendall(b"hi"))

    with net.create_connection(listener.getsockname(), timeout=5) as connection:
        assert connection.recv(10) == b"hi" and type(connection) is socket.socket


def test_a_connection_goes_through_connect_and_is_a_normal_socket_whose_peer_is_the_destination():
    echo = serve(lambda c: [c.sendall(d.upper()) for d in iter(lambda: c.recv(100), b"")])
    proxy = Proxy({"files.example.test:2121": echo.getsockname()})
    net.use_proxy(proxy.address)

    with net.create_connection(("files.example.test", 2121), timeout=5) as connection:
        connection.sendall(b"hello")
        assert connection.recv(100) == b"HELLO"
        assert connection.getpeername() == ("files.example.test", 2121), "not the loopback of the proxy"
        assert connection.gettimeout() == 5 and isinstance(connection, socket.socket)
    assert proxy.asked == ["files.example.test:2121"]


def test_what_the_server_says_first_stays_in_the_socket():
    greeting = serve(lambda c: c.sendall(b"220 welcome\r\n"))
    proxy = Proxy({"ftp.test:21": greeting.getsockname()})
    net.use_proxy(proxy.address)

    with net.create_connection(("ftp.test", 21), timeout=5) as connection:
        assert connection.makefile("rb").readline() == b"220 welcome\r\n"


def test_a_proxy_that_sends_the_banner_with_its_answer_loses_none_of_it():
    def proxy_handler(connection):
        read_head(connection)
        connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n220 in the same packet\r\n")

    net.use_proxy(serve(proxy_handler).getsockname())

    with net.create_connection(("ftp.test", 21), timeout=5) as connection:
        assert connection.recv(100) == b"220 in the same packet\r\n"


def test_a_destination_the_proxy_refuses_is_a_permission_error_with_the_reason():
    proxy = Proxy()
    net.use_proxy(proxy.address)

    with pytest.raises(net.ProxyRefused, match="not allowed for this provider") as raised:
        net.create_connection(("evil.example", 22), timeout=5)

    assert isinstance(raised.value, PermissionError) and isinstance(raised.value, OSError)


def test_a_destination_the_proxy_cannot_reach_is_a_connection_error():
    proxy = Proxy(dead={"down.test:21"})
    net.use_proxy(proxy.address)

    with pytest.raises(net.ProxyUnreachable, match="502") as raised:
        net.create_connection(("down.test", 21), timeout=5)

    assert isinstance(raised.value, ConnectionError)


def test_a_proxy_that_hangs_up_or_speaks_nonsense_is_a_connection_error():
    net.use_proxy(serve(lambda c: read_head(c)).getsockname())
    with pytest.raises(net.ProxyUnreachable, match="closed"):
        net.create_connection(("a.test", 1), timeout=5)

    net.use_proxy(serve(lambda c: (read_head(c), c.sendall(b"SSH-2.0-nonsense\r\n\r\n"))).getsockname())
    with pytest.raises(net.ProxyUnreachable, match="not an HTTP response"):
        net.create_connection(("a.test", 1), timeout=5)


def test_nothing_the_caller_passes_can_inject_into_the_request():
    proxy = Proxy()
    net.use_proxy(proxy.address)

    for address in (("a.test\r\nX: y", 21), ("a b", 21), ("", 21), ("a.test", 0), ("a.test", 70000), ("a.test", "21"), (b"a.test", 21)):
        with pytest.raises(ValueError):
            net.create_connection(address)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        net.create_connection(("a.test", 21), source_address=("127.0.0.1", 0))
    assert proxy.asked == []


def test_an_ipv6_destination_is_written_in_brackets():
    proxy = Proxy({"[fd00::1]:21": serve(lambda c: None).getsockname()})
    net.use_proxy(proxy.address)

    net.create_connection(("fd00::1", 21), timeout=5).close()

    assert proxy.asked == ["[fd00::1]:21"]


# ------------------------------------------------------------------
# ftplib, control and data connections
# ------------------------------------------------------------------


class FakeFtpServer:
    """Just enough FTP for ``ftplib``: a greeting, USER/PASS/TYPE, passive mode with the data connection on a port
    the server picks (and a private address in the 227 reply), NLST and RETR."""

    FILES = {"song.flac": b"fLaC" + bytes(range(256)) * 40}

    def __init__(self):
        self.data_listeners: list[socket.socket] = []
        self.listener = serve(self.control)

    @property
    def port(self):
        return self.listener.getsockname()[1]

    def control(self, connection):
        stream = connection.makefile("rwb")

        def reply(text):
            stream.write(text.encode() + b"\r\n")
            stream.flush()

        reply("220 fake ftp ready")
        data_listener = None
        while line := stream.readline():
            command, _, argument = line.decode().strip().partition(" ")
            if command == "USER":
                reply("331 password please")
            elif command == "PASS":
                reply("230 welcome")
            elif command in ("TYPE", "OPTS"):
                reply("200 ok")
            elif command == "PASV":
                data_listener = socket.socket()
                data_listener.bind(("127.0.0.1", 0))
                data_listener.listen(1)
                self.data_listeners.append(data_listener)
                port = data_listener.getsockname()[1]
                reply(f"227 Entering Passive Mode (10,0,0,5,{port // 256},{port % 256})")  # a private address: not to be trusted
            elif command in ("NLST", "RETR"):
                payload = "\n".join(self.FILES).encode() if command == "NLST" else self.FILES[argument]
                reply("150 here it comes")
                data, _ = data_listener.accept()
                with data:
                    data.sendall(payload)
                reply("226 done")
            elif command == "QUIT":
                reply("221 bye")
                return
            else:
                reply("502 not implemented")


@pytest.fixture
def routed_ftplib():
    original = ftplib.socket
    yield
    ftplib.socket = original


def test_ftplib_opens_its_control_and_its_data_connections_through_the_proxy(routed_ftplib):
    server = FakeFtpServer()
    # The names do not resolve: a connection that did not go through the proxy cannot succeed.
    proxy = Proxy(hosts={"files.example.test": "127.0.0.1"})
    net.use_proxy(proxy.address)
    net.route_module(ftplib)

    ftp = ftplib.FTP()
    ftp.connect("files.example.test", server.port, timeout=5)
    ftp.login("user", "secret")
    # The 227 reply names 10.0.0.5, which is not trusted: the data connection goes to the host of the control one.
    names = ftp.nlst()
    chunks: list[bytes] = []
    ftp.retrbinary("RETR song.flac", chunks.append)
    ftp.quit()

    assert names == ["song.flac"] and b"".join(chunks) == FakeFtpServer.FILES["song.flac"]
    data_ports = [listener.getsockname()[1] for listener in server.data_listeners]
    assert len(data_ports) == 2
    assert proxy.asked == [f"files.example.test:{server.port}", *(f"files.example.test:{port}" for port in data_ports)], (
        "one CONNECT for the control connection and one for each data connection"
    )


def test_a_proxy_that_does_not_allow_the_data_port_stops_the_transfer_cleanly(routed_ftplib):
    server = FakeFtpServer()
    proxy = Proxy({f"files.example.test:{server.port}": ("127.0.0.1", server.port)})  # the data ports are not routed
    net.use_proxy(proxy.address)
    net.route_module(ftplib)
    ftp = ftplib.FTP()
    ftp.connect("files.example.test", server.port, timeout=5)
    ftp.login("user", "secret")

    with pytest.raises(PermissionError, match="not allowed"):
        ftp.nlst()


def test_routing_a_module_changes_that_module_only(routed_ftplib):
    net.route_module(ftplib)
    net.route_module(ftplib)  # again: nothing piles up

    assert ftplib.socket.create_connection is net.create_connection
    assert ftplib.socket.AF_INET == socket.AF_INET and ftplib.socket.timeout is socket.timeout
    assert socket.create_connection is not net.create_connection, "the rest of the process keeps the real socket"


def test_only_a_module_that_uses_socket_can_be_routed():
    import json

    with pytest.raises(TypeError):
        net.route_module(json)
