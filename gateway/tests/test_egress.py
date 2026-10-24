"""What a sandbox may reach: the decisions, and the proxy that is its only way out."""
from __future__ import annotations

import socket
import threading

import pytest

from musizeer_gateway import egress
from musizeer_gateway.egress import EgressPolicy, EgressServer, classify, resolve
from musizeer_sdk.permissions import PermissionsError, TcpAccess


# ------------------------------------------------------------------
# Decisions
# ------------------------------------------------------------------


def test_a_fixed_destination_is_allowed_and_nothing_else_is():
    policy = EgressPolicy("p", ["lrclib.net:443"])

    assert policy.decide("lrclib.net", 443).allowed
    assert policy.decide("LRCLIB.net.", 443).allowed, "case and a trailing dot do not matter"
    assert not policy.decide("lrclib.net", 80).allowed
    assert "port 80 is not allowed" in policy.decide("lrclib.net", 80).reason
    assert not policy.decide("example.com", 443).allowed
    assert not policy.decide("sub.lrclib.net", 443).allowed, "a subdomain is not the declared host"
    assert not policy.decide("not a host", 443).allowed


def test_a_fixed_destination_never_reaches_a_private_address_but_a_named_one_may():
    policy = EgressPolicy("p", ["lrclib.net:443"])
    policy.grant(["192.168.1.5:4533", "navidrome.lan:4533"], ttl=60)

    assert policy.decide("lrclib.net", 443).private_ok is False
    assert policy.decide("192.168.1.5", 4533) .allowed and policy.decide("192.168.1.5", 4533).private_ok
    assert policy.decide("navidrome.lan", 4533).private_ok
    assert not policy.decide("192.168.1.5", 22).allowed, "the port the setting names, not any port"
    assert not policy.decide("192.168.1.6", 4533).allowed


def test_a_grant_replaces_the_previous_one_and_expires():
    now = [0.0]
    policy = EgressPolicy("p", [], clock=lambda: now[0])

    assert policy.grant(["a.lan:1", "b.lan:2", "garbage", "c.lan:99999"], ttl=30) == 2
    assert policy.decide("a.lan", 1).allowed
    policy.grant(["b.lan:2"], ttl=30)
    assert not policy.decide("a.lan", 1).allowed and policy.decide("b.lan", 2).allowed
    now[0] = 31.0
    assert not policy.decide("b.lan", 2).allowed, "a host that stops asking withdraws the grant"


def test_the_number_of_grants_is_bounded():
    policy = EgressPolicy("p", [])

    assert policy.grant([f"h{i}.lan:1" for i in range(500)], ttl=10) == egress.MAX_GRANTS


@pytest.mark.parametrize("address, kind", [
    ("93.184.216.34", "public"), ("2606:4700::1111", "public"),
    ("10.0.0.1", "private"), ("172.16.5.5", "private"), ("192.168.1.1", "private"), ("fd00::1", "private"),
    ("100.64.0.7", "private"), ("100.100.100.100", "private"),
    ("127.0.0.1", "never"), ("::1", "never"), ("169.254.169.254", "never"), ("fe80::1", "never"),
    ("0.0.0.0", "never"), ("224.0.0.1", "never"), ("::ffff:127.0.0.1", "never"), ("::ffff:10.0.0.1", "private"),
])
def test_addresses_are_public_private_or_never(address, kind):
    assert classify(address) == kind


def fake_dns(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, 1, 6, "", (a, port)) for a in addresses])


def test_a_name_that_resolves_to_a_private_address_is_refused_unless_it_was_named_by_a_setting(monkeypatch):
    fake_dns(monkeypatch, "10.1.2.3")

    with pytest.raises(PermissionsError, match="may not be reached"):
        resolve("lrclib.net", 443, private_ok=False)
    assert resolve("navidrome.lan", 4533, private_ok=True) == ["10.1.2.3"]


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "0.0.0.0"])
def test_loopback_and_link_local_are_unreachable_even_when_named(monkeypatch, address):
    fake_dns(monkeypatch, address)

    with pytest.raises(PermissionsError):
        resolve("whatever.lan", 80, private_ok=True)


def test_only_the_reachable_addresses_of_a_name_are_used(monkeypatch):
    fake_dns(monkeypatch, "127.0.0.1", "93.184.216.34", "93.184.216.34")

    assert resolve("mixed.example", 443, private_ok=False) == ["93.184.216.34"]


def test_a_name_that_does_not_resolve_is_refused_with_a_reason(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(PermissionsError, match="cannot resolve"):
        resolve("nowhere.invalid", 443, False)


# ------------------------------------------------------------------
# The proxy
# ------------------------------------------------------------------


@pytest.fixture
def upstream():
    """A TCP server on the loopback that answers like a web server and records what it was sent."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    received: list[bytes] = []

    def serve():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(0.2)
                try:
                    data = connection.recv(4096)
                except OSError:
                    data = b""
                if data:
                    received.append(data)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1], received
    listener.close()


@pytest.fixture
def proxy(tmp_path):
    servers = []

    def start(policy: EgressPolicy, resolver=lambda host, port, private_ok: ["127.0.0.1"]) -> str:
        path = tmp_path / f"egress{len(servers)}.sock"
        server = EgressServer(path, policy, resolver)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return str(path)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def ask(path: str, request: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(path)
        client.sendall(request)
        data = b""
        while chunk := client.recv(4096):
            data += chunk
        return data


def test_connect_to_a_declared_destination_is_tunnelled(proxy, upstream):
    port, received = upstream
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    reply = ask(path, f"CONNECT api.example.org:{port} HTTP/1.1\r\nHost: api.example.org\r\n\r\nGET / HTTP/1.1\r\n\r\n".encode())

    assert reply.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and reply.endswith(b"OK")
    assert received == [b"GET / HTTP/1.1\r\n\r\n"]


def test_connect_to_an_undeclared_host_or_port_is_refused_and_logged(proxy, upstream, caplog):
    port, received = upstream
    policy = EgressPolicy("p", [f"api.example.org:{port}"])
    path = proxy(policy)

    with caplog.at_level("WARNING"):
        other_host = ask(path, f"CONNECT evil.example:{port} HTTP/1.1\r\n\r\n".encode())
        other_port = ask(path, b"CONNECT api.example.org:22 HTTP/1.1\r\n\r\n")
        direct_ip = ask(path, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())

    for reply in (other_host, other_port, direct_ip):
        assert reply.startswith(b"HTTP/1.1 403 Forbidden")
    assert received == [], "nothing reached the upstream"
    assert [r.target for r in policy.refused] == [f"evil.example:{port}", "api.example.org:22", f"127.0.0.1:{port}"]
    assert "egress refused: provider=p target=evil.example" in caplog.text


def test_a_destination_a_setting_names_is_reachable_only_while_it_is_granted(proxy, upstream):
    port, _ = upstream
    policy = EgressPolicy("p", [])
    path = proxy(policy)
    request = f"CONNECT navidrome.lan:{port} HTTP/1.1\r\n\r\n".encode()

    assert ask(path, request).startswith(b"HTTP/1.1 403")
    policy.grant([f"navidrome.lan:{port}"], ttl=60)
    assert ask(path, request).startswith(b"HTTP/1.1 200")
    policy.grant([], ttl=60)
    assert ask(path, request).startswith(b"HTTP/1.1 403")


def test_the_resolver_is_told_whether_a_private_address_may_be_used(proxy, upstream):
    port, _ = upstream
    seen = []

    def resolver(host, port_, private_ok):
        seen.append((host, private_ok))
        return ["127.0.0.1"]

    policy = EgressPolicy("p", [f"fixed.example:{port}"])
    policy.grant([f"named.lan:{port}"], ttl=60)
    path = proxy(policy, resolver)

    ask(path, f"CONNECT fixed.example:{port} HTTP/1.1\r\n\r\n".encode())
    ask(path, f"CONNECT named.lan:{port} HTTP/1.1\r\n\r\n".encode())

    assert seen == [("fixed.example", False), ("named.lan", True)]


def test_a_destination_that_resolves_to_something_unreachable_is_refused(proxy, upstream):
    port, received = upstream

    def resolver(host, port_, private_ok):
        raise PermissionsError("fixed.example resolves only to addresses that may not be reached: 10.0.0.1 (private)")

    policy = EgressPolicy("p", [f"fixed.example:{port}"])
    reply = ask(proxy(policy, resolver), f"CONNECT fixed.example:{port} HTTP/1.1\r\n\r\n".encode())

    assert reply.startswith(b"HTTP/1.1 403") and b"may not be reached" in reply
    assert received == [] and "may not be reached" in policy.refused[0].reason


def test_plain_http_is_forwarded_in_origin_form_and_closed_after_one_request(proxy, upstream):
    port, received = upstream
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    reply = ask(path, (
        f"GET http://api.example.org:{port}/lyrics?q=a HTTP/1.1\r\nHost: api.example.org\r\n"
        "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nProxy-Authorization: x\r\nAccept: */*\r\n\r\n"
    ).encode())

    assert reply.endswith(b"OK")
    sent = received[0].decode()
    assert sent.startswith("GET /lyrics?q=a HTTP/1.1\r\n") and "Connection: close" in sent
    assert "Proxy-" not in sent and "keep-alive" not in sent and "Accept: */*" in sent


def test_plain_http_to_an_undeclared_host_is_refused(proxy, upstream):
    port, received = upstream
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    assert ask(path, f"GET http://evil.example:{port}/ HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
    assert ask(path, b"GET /relative HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert ask(path, b"GET https://api.example.org/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert received == []


def test_garbage_is_answered_with_a_bad_request(proxy):
    path = proxy(EgressPolicy("p", []))

    assert ask(path, b"\x00\x01nonsense\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert ask(path, b"CONNECT nonsense HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")


def test_a_destination_that_cannot_be_connected_is_a_bad_gateway(proxy):
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    for request in (
        f"CONNECT api.example.org:{port} HTTP/1.1\r\n\r\n",
        f"GET http://api.example.org:{port}/ HTTP/1.1\r\nHost: api.example.org\r\n\r\n",
    ):
        reply = ask(path, request.encode())
        assert reply.startswith(b"HTTP/1.1 502")
        assert b"\r\nX-Musizeer-Egress: unreachable\r\n" in reply.split(b"\r\n\r\n", 1)[0] + b"\r\n"


def test_a_refusal_is_marked_as_the_proxys_own_answer(proxy, upstream):
    port, received = upstream
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    for request in (
        f"CONNECT evil.example:{port} HTTP/1.1\r\n\r\n",
        f"GET http://evil.example:{port}/ HTTP/1.1\r\n\r\n",
    ):
        reply = ask(path, request.encode())
        assert reply.startswith(b"HTTP/1.1 403")
        assert b"\r\nX-Musizeer-Egress: refused\r\n" in reply.split(b"\r\n\r\n", 1)[0] + b"\r\n"
    assert received == []


def test_an_answer_of_the_destination_carries_no_marker(proxy, upstream):
    port, _received = upstream
    path = proxy(EgressPolicy("p", [f"api.example.org:{port}"]))

    reply = ask(path, f"GET http://api.example.org:{port}/ HTTP/1.1\r\nHost: api.example.org\r\n\r\n".encode())

    assert b"X-Musizeer-Egress" not in reply


def test_the_proxy_socket_is_the_owners_alone(proxy):
    import os

    path = proxy(EgressPolicy("p", []))

    assert os.stat(path).st_mode & 0o777 == 0o600


# ------------------------------------------------------------------
# Wildcards: any subdomain, never the domain itself, never a private address
# ------------------------------------------------------------------


def test_a_wildcard_allows_any_subdomain_on_its_port_and_nothing_else():
    policy = EgressPolicy("p", ["*.cdn.example:443"])

    for host in ("a.cdn.example", "a.b.cdn.example", "A.CDN.example."):
        assert policy.decide(host, 443).allowed, host
    assert not policy.decide("cdn.example", 443).allowed, "the domain itself is not a subdomain of itself"
    assert not policy.decide("a.cdn.example", 80).allowed
    assert "port 80 is not allowed" in policy.decide("a.cdn.example", 80).reason
    assert not policy.decide("evilcdn.example", 443).allowed, "a longer name that merely ends the same way"
    assert not policy.decide("a.cdn.example.evil.org", 443).allowed
    assert not policy.decide("example", 443).allowed
    assert policy.fixed == frozenset({("*.cdn.example", 443)})


def test_the_domain_itself_is_listed_on_its_own():
    policy = EgressPolicy("p", ["*.cdn.example:443", "cdn.example:443"])

    assert policy.decide("cdn.example", 443).allowed and policy.decide("x.cdn.example", 443).allowed


def test_a_wildcard_match_is_held_to_public_addresses_only():
    policy = EgressPolicy("p", ["*.cdn.example:443"])

    assert policy.decide("a.cdn.example", 443).private_ok is False


def test_an_address_never_matches_a_wildcard():
    policy = EgressPolicy("p", ["*.cdn.example:443"])

    assert not policy.decide("1.2.3.4", 443).allowed and not policy.decide("127.0.0.1", 443).allowed


@pytest.mark.parametrize("entry", ["*:443", "*.com:443", "*.:443", "a.*.example.com:443", "*.*.example.com:443", "*.1.2.3.4:443", "**.example.com:443", "*example.com:443"])
def test_a_wildcard_that_is_too_wide_or_malformed_is_not_a_policy(entry):
    with pytest.raises(PermissionsError):
        EgressPolicy("p", [entry])


def test_a_grant_can_never_be_a_wildcard():
    policy = EgressPolicy("p", [])

    assert policy.grant(["*.example.com:443", "a.example.com:443"], ttl=60) == 1
    assert not policy.decide("b.example.com", 443).allowed


def test_a_wildcard_subdomain_that_resolves_to_a_private_address_is_refused(proxy, upstream, monkeypatch):
    port, received = upstream
    policy = EgressPolicy("p", [f"*.cdn.example:{port}"])
    fake_dns(monkeypatch, "10.0.0.7")

    reply = ask(proxy(policy, resolve), f"CONNECT evil.cdn.example:{port} HTTP/1.1\r\n\r\n".encode())

    assert reply.startswith(b"HTTP/1.1 403") and b"may not be reached" in reply and received == []


def test_a_wildcard_subdomain_is_tunnelled_through_the_proxy(proxy, upstream):
    port, received = upstream
    policy = EgressPolicy("p", [f"*.cdn.example:{port}"])

    reply = ask(proxy(policy), f"CONNECT media.cdn.example:{port} HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\n\r\n".encode())

    assert reply.startswith(b"HTTP/1.1 200") and received == [b"GET / HTTP/1.1\r\n\r\n"]
    assert ask(proxy(policy), f"CONNECT cdn.example:{port} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")


# ------------------------------------------------------------------
# Raw TCP to the host a setting names, on any port or a declared range
# ------------------------------------------------------------------


def test_the_host_a_tcp_setting_names_is_reachable_on_any_port_of_the_manifest():
    policy = EgressPolicy("p", [], tcp=[TcpAccess("host", "any"), TcpAccess("data", "30000-30100")])
    policy.grant([], 60, {"host": ["ftp.lan"], "data": ["pasv.lan"]})

    assert policy.decide("ftp.lan", 21).allowed and policy.decide("ftp.lan", 47123).allowed and policy.decide("ftp.lan", 65535).allowed
    assert policy.decide("ftp.lan", 21).private_ok, "the user typed the host: a LAN address is fine"
    assert policy.decide("pasv.lan", 30000).allowed and policy.decide("pasv.lan", 30100).allowed
    assert not policy.decide("pasv.lan", 29999).allowed and not policy.decide("pasv.lan", 30101).allowed
    assert "port 21 is not allowed" in policy.decide("pasv.lan", 21).reason
    assert not policy.decide("other.lan", 21).allowed, "only the hosts the settings name"


def test_a_setting_the_manifest_does_not_declare_grants_nothing_and_ports_are_the_manifests_not_the_settings():
    policy = EgressPolicy("p", [], tcp=[TcpAccess("host", "2121")])
    policy.grant([], 60, {"host": ["ftp.lan"], "undeclared": ["evil.lan"]})

    assert policy.decide("ftp.lan", 2121).allowed and not policy.decide("ftp.lan", 22).allowed
    assert not policy.decide("evil.lan", 2121).allowed
    assert policy.granted_tcp() == frozenset({("ftp.lan", 2121, 2121)})


def test_a_tcp_grant_is_replaced_and_expires_like_the_others():
    now = [0.0]
    policy = EgressPolicy("p", [], clock=lambda: now[0], tcp=[TcpAccess("host")])

    policy.grant([], 30, {"host": ["a.lan"]})
    assert policy.decide("a.lan", 9).allowed
    policy.grant([], 30, {"host": ["b.lan"]})
    assert not policy.decide("a.lan", 9).allowed and policy.decide("b.lan", 9).allowed
    now[0] = 31.0
    assert not policy.decide("b.lan", 9).allowed
    policy.grant([], 30)
    assert policy.granted_tcp() == frozenset()


def test_a_tcp_host_that_is_not_a_host_grants_nothing():
    policy = EgressPolicy("p", [], tcp=[TcpAccess("host")])

    policy.grant([], 60, {"host": ["*.example.com", "a b", "", "ok.lan"]})

    assert policy.granted_tcp() == frozenset({("ok.lan", 1, 65535)})


def test_a_tcp_destination_still_never_reaches_loopback_or_metadata(monkeypatch):
    fake_dns(monkeypatch, "169.254.169.254")

    with pytest.raises(PermissionsError):
        resolve("ftp.lan", 21, private_ok=True)


def test_raw_tcp_is_tunnelled_on_a_port_the_server_chose(proxy, upstream):
    port, received = upstream
    policy = EgressPolicy("p", [], tcp=[TcpAccess("host", "any")])
    policy.grant([], 60, {"host": ["ftp.lan"]})

    reply = ask(proxy(policy), f"CONNECT ftp.lan:{port} HTTP/1.1\r\n\r\nPASV\r\n".encode())

    assert reply.startswith(b"HTTP/1.1 200") and received == [b"PASV\r\n"]
