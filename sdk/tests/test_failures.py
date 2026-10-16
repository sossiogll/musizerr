"""
The classifier tells a failure by the type of the exception, without importing
the libraries that raise it: the classes below imitate the ones of httpx,
requests and urllib by name and by shape, which is all it looks at.
"""
from __future__ import annotations

import socket
import urllib.error
from types import SimpleNamespace

import pytest

from musizerr_sdk.errors import ProviderRateLimited
from musizerr_sdk.failures import Failure, classify_exception

# --- httpx-like -------------------------------------------------------------


class HTTPStatusError(Exception):
    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(f"Client error '{status}' for url 'https://api.example/x?key=secret'")
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class TransportError(Exception): ...
class TimeoutException(TransportError): ...
class ConnectTimeout(TimeoutException): ...
class ReadTimeout(TimeoutException): ...
class NetworkError(TransportError): ...
class ConnectError(NetworkError): ...
class ReadError(NetworkError): ...
class RemoteProtocolError(TransportError): ...

# --- requests-like ----------------------------------------------------------


class RequestException(OSError): ...
class RequestsHTTPError(RequestException):
    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(f"{status} Error")
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


RequestsHTTPError.__name__ = "HTTPError"


class Timeout(RequestException): ...
class RequestsConnectionError(RequestException): ...
RequestsConnectionError.__name__ = "ConnectionError"
class RequestsConnectTimeout(RequestsConnectionError, Timeout): ...
RequestsConnectTimeout.__name__ = "ConnectTimeout"


@pytest.mark.parametrize("status, kind", [
    (401, "auth_failed"), (403, "auth_failed"), (404, "not_found"), (429, "rate_limited"),
    (500, "upstream_error"), (502, "upstream_error"), (503, "upstream_error"), (400, "failed"), (418, "failed"),
])
@pytest.mark.parametrize("make", [HTTPStatusError, RequestsHTTPError], ids=["httpx-like", "requests-like"])
def test_an_http_status_is_told_by_its_status(make, status, kind):
    assert classify_exception(make(status)).kind == kind


@pytest.mark.parametrize("make", [
    lambda: TimeoutError("timed out"),
    lambda: socket.timeout("timed out"),
    lambda: ReadTimeout("slow"),
    lambda: ConnectTimeout("slow"),
    lambda: Timeout("slow"),
    lambda: RequestsConnectTimeout("slow"),
    lambda: urllib.error.URLError(TimeoutError("timed out")),
])
def test_a_timeout_is_a_timeout(make):
    assert classify_exception(make()).kind == "timeout"


@pytest.mark.parametrize("make", [
    lambda: ConnectError("refused"),
    lambda: ReadError("reset"),
    lambda: RequestsConnectionError("refused"),
    lambda: ConnectionRefusedError(111, "Connection refused"),
    lambda: ConnectionResetError("reset"),
    lambda: BrokenPipeError("pipe"),
    lambda: socket.gaierror(-3, "Temporary failure in name resolution"),
    lambda: urllib.error.URLError("no route"),
])
def test_the_network_being_down_is_unreachable(make):
    assert classify_exception(make()).kind == "unreachable"


@pytest.mark.parametrize("make", [HTTPStatusError, RequestsHTTPError], ids=["httpx-like", "requests-like"])
def test_the_egress_proxy_answering_for_a_destination_that_does_not_answer_is_unreachable_not_its_502(make):
    assert classify_exception(make(502, {"X-Musizerr-Egress": "unreachable"})).kind == "unreachable"
    assert classify_exception(make(502, {"x-musizerr-egress": "unreachable"})).kind == "unreachable"
    assert classify_exception(make(502)).kind == "upstream_error"


@pytest.mark.parametrize("make", [HTTPStatusError, RequestsHTTPError], ids=["httpx-like", "requests-like"])
def test_the_egress_proxy_refusing_a_destination_is_a_failure_not_a_refused_credential(make):
    assert classify_exception(make(403, {"X-Musizerr-Egress": "refused"})).kind == "failed"
    assert classify_exception(make(403)).kind == "auth_failed"


def test_a_header_value_the_proxy_does_not_give_changes_nothing():
    assert classify_exception(HTTPStatusError(502, {"X-Musizerr-Egress": "maybe"})).kind == "upstream_error"


class ProxyError(TransportError): ...


def test_an_https_tunnel_the_proxy_did_not_open_is_told_by_the_proxy_status():
    assert classify_exception(ProxyError("502 Bad Gateway")).kind == "unreachable"
    assert classify_exception(ProxyError("403 Forbidden")).kind == "failed"


def test_urllib_http_errors_are_told_by_their_code():
    error = urllib.error.HTTPError("https://x/y", 404, "Not Found", {}, None)

    assert classify_exception(error).kind == "not_found"
    assert classify_exception(urllib.error.HTTPError("https://x/y", 503, "Unavailable", {}, None)).kind == "upstream_error"


@pytest.mark.parametrize("exc", [
    RuntimeError("Lidarr URL not configured"), ValueError("bad"), KeyError("k"), RemoteProtocolError("odd"),
    FileNotFoundError("no such file"),
])
def test_what_has_no_recognisable_type_has_no_kind(exc):
    failure = classify_exception(exc)

    assert failure.kind is None
    assert failure.message


def test_the_message_is_the_text_of_the_error_or_its_class_when_there_is_none():
    assert classify_exception(RuntimeError("boom")).message == "boom"
    assert classify_exception(RuntimeError()).message == "RuntimeError"


def test_a_rate_limit_of_the_contract_keeps_its_retry_after():
    assert classify_exception(ProviderRateLimited("slow", retry_after=9)) == Failure("rate_limited", "slow", 9)
    assert classify_exception(ProviderRateLimited("slow")).retry_after is None


@pytest.mark.parametrize("header, seconds", [("30", 30.0), ("2.5", 2.5), ("0", None), ("-3", None), ("soon", None), ("nan", None)])
def test_a_429_hands_over_the_retry_after_header_when_it_is_a_number_of_seconds(header, seconds):
    for headers in ({"retry-after": header}, {"Retry-After": header}):
        failure = classify_exception(HTTPStatusError(429, headers))
        assert (failure.kind, failure.retry_after) == ("rate_limited", seconds)


def test_a_429_without_the_header_has_no_retry_after():
    assert classify_exception(HTTPStatusError(429)).retry_after is None
    assert classify_exception(RequestsHTTPError(429)).retry_after is None


def test_an_object_that_merely_has_a_response_without_a_status_is_not_an_http_error():
    error = RuntimeError("x")
    error.response = SimpleNamespace(status_code="oops")

    assert classify_exception(error).kind is None


def test_a_timeout_wins_over_the_network_and_the_network_over_nothing():
    assert classify_exception(ConnectTimeout("x")).kind == "timeout"
    assert classify_exception(ConnectError("x")).kind == "unreachable"
