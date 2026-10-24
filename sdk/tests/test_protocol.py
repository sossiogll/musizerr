"""Frames, requests, and the errors that cross the protocol."""
from __future__ import annotations

import io
import socket
import struct

import pytest
from hypothesis import given, settings, strategies as st

from musizeer_sdk import protocol, wire
from musizeer_sdk.auth import AuthFlow, AuthSession
from musizeer_sdk.context import ContextSnapshot
from musizeer_sdk.errors import (
    DelegateHoldsRelease,
    DownloadDeclined,
    DownloadDelegated,
    ProviderRateLimited,
    RemotePlaylistGone,
)
from musizeer_sdk.interfaces import StorageStreamResult
from musizeer_sdk.models import Track
from musizeer_sdk.protocol import FRAME_BYTES, FRAME_JSON, Frame, FrameSocket, ProtocolError

frames = st.builds(Frame, kind=st.sampled_from([FRAME_JSON, FRAME_BYTES]), payload=st.binary(max_size=300))


def reader(data: bytes, step: int | None = None):
    """``read(n)`` over ``data``, handing out at most ``step`` bytes at a time
    (a socket does not return a frame in one piece)."""
    stream = io.BytesIO(data)
    return lambda n: stream.read(n if step is None else min(n, step))


# --- frames -----------------------------------------------------------------


def test_a_frame_is_a_big_endian_length_a_type_byte_and_the_payload():
    packed = protocol.pack_frame(Frame(FRAME_JSON, b"{}"))

    assert packed == struct.pack(">I", 3) + bytes([FRAME_JSON]) + b"{}"
    assert protocol.pack_frame(Frame(FRAME_BYTES, b"")) == struct.pack(">I", 1) + bytes([FRAME_BYTES])


@settings(max_examples=80, deadline=None)
@given(st.lists(frames, max_size=6), st.sampled_from([None, 1, 3]))
def test_frames_read_back_in_order_even_when_they_arrive_in_pieces(sent, step):
    data = b"".join(protocol.pack_frame(frame) for frame in sent)
    read = reader(data, step)

    received = []
    while (frame := protocol.read_frame(read)) is not None:
        received.append(frame)

    assert received == sent


def test_the_end_of_the_stream_between_frames_is_not_an_error():
    assert protocol.read_frame(reader(b"")) is None


@pytest.mark.parametrize("cut", [1, 4, 5, 6])
def test_the_end_of_the_stream_inside_a_frame_is_an_error(cut):
    data = protocol.pack_frame(Frame(FRAME_JSON, b"abcdef"))[:cut]

    with pytest.raises(ProtocolError, match="middle of a frame"):
        protocol.read_frame(reader(data))


def test_an_empty_an_oversized_or_unknown_frame_is_refused():
    for header in (struct.pack(">IB", 0, FRAME_JSON), struct.pack(">IB", protocol.MAX_FRAME_SIZE + 1, FRAME_JSON),
                   struct.pack(">IB", 1, 9)):
        with pytest.raises(ProtocolError):
            protocol.read_frame(reader(header))
    with pytest.raises(ProtocolError):
        protocol.pack_frame(Frame(9, b""))
    with pytest.raises(ProtocolError):
        protocol.pack_frame(Frame(FRAME_BYTES, bytes(protocol.MAX_FRAME_SIZE)))


def test_frames_travel_over_a_socket_pair_in_both_directions():
    left, right = socket.socketpair()
    with left, right:
        a, b = FrameSocket(left), FrameSocket(right)
        big = Frame(FRAME_BYTES, bytes(range(256)) * 150)

        a.send(protocol.json_frame({"hello": "there"}))
        a.send(big)
        b.send(protocol.bytes_frame(b"pong"))

        assert protocol.load_message(b.receive()) == {"hello": "there"}
        assert b.receive() == big
        assert a.receive() == Frame(FRAME_BYTES, b"pong")
        left.shutdown(socket.SHUT_WR)
        assert b.receive() is None


def test_a_json_frame_must_hold_a_json_object():
    for frame in (Frame(FRAME_BYTES, b"{}"), Frame(FRAME_JSON, b"[1]"), Frame(FRAME_JSON, b"nope")):
        with pytest.raises(ProtocolError):
            protocol.load_message(frame)


# --- requests ---------------------------------------------------------------


def test_the_session_is_carried_apart_and_put_back_where_it_was():
    session = AuthSession(flow=AuthFlow.NONE, extra={"a": 1})

    for args, kwargs, at in (
        (("query", session, 20), {}, 1),
        ((session, "x"), {"limit": 3}, 0),
        (("x",), {"session": session, "limit": 3}, None),
    ):
        rest_args, rest_kwargs, found, found_at = protocol.split_session(args, kwargs)
        assert found is session and found_at == at
        assert session not in rest_args and "session" not in rest_kwargs
        assert protocol.join_session(rest_args, rest_kwargs, found, found_at) == (list(args), dict(kwargs))


def test_a_call_without_a_session_has_none():
    assert protocol.split_session(("x", 1), {"k": 2}) == (["x", 1], {"k": 2}, None, None)
    assert protocol.join_session(["x"], {}, None, None) == (["x"], {})


def test_a_call_request_carries_the_method_the_arguments_the_session_and_the_context_as_wire_values():
    session = AuthSession(flow=AuthFlow.OAUTH2, headers={"Authorization": "Bearer t"})
    context = ContextSnapshot(settings={"market": "IT"}, api_prefix="/api")

    message = protocol.parse_request(protocol.call_request("search_tracks", ("q", session, 5), {}, context))

    assert message["op"] == "call" and message["method"] == "search_tracks" and message["v"] == protocol.PROTOCOL_VERSION
    assert message["args"] == ["q", 5] and message["kwargs"] == {} and message["session_at"] == 1
    assert wire.decode(message["session"]) == session
    assert wire.decode(message["context"]) == context


def test_a_request_of_another_version_or_operation_is_refused():
    for message in ({"v": 99, "op": "call"}, {"v": 1, "op": "shell"}, {"op": "call"}):
        with pytest.raises(ProtocolError):
            protocol.parse_request(protocol.json_frame(message))


# --- errors -----------------------------------------------------------------


def fail(error: protocol.RemoteError) -> BaseException:
    return RuntimeError(f"remote {error.kind}")


TRACK = Track(title="T", artist="A", album="B", duration_ms=1)

CONTRACT = [
    DownloadDelegated("p", ["queued", {"id": 1}], handle="h"),
    DownloadDeclined("p", "have_release", "already there"),
    DelegateHoldsRelease("p", "h", [TRACK], complete=True, release="Album", details=["x"], state={"k": [1]}),
    RemotePlaylistGone("gone"),
    ProviderRateLimited("slow down", retry_after=7.5),
    ProviderRateLimited("slow down"),
    PermissionError("missing scope"),
    NotImplementedError("nope"),
]


@pytest.mark.parametrize("exc", CONTRACT, ids=lambda e: f"{type(e).__name__}-{str(e)[:12]}")
def test_the_exceptions_of_the_contract_cross_as_themselves(exc):
    body = protocol.error_body(exc)

    raised = protocol.exception_from_body(wire.loads(wire.dumps(body)), fail)

    assert type(raised) is type(exc)
    assert raised.args == exc.args
    assert vars(raised) == vars(exc)


def test_a_subclass_of_a_contract_exception_crosses_as_the_contract_class():
    class Quota(ProviderRateLimited): ...

    raised = protocol.exception_from_body(protocol.error_body(Quota("q", retry_after=3)), fail)

    assert type(raised) is ProviderRateLimited and raised.retry_after == 3


def test_any_other_exception_crosses_as_a_kind_a_message_and_a_retry_after_and_nothing_else():
    class Weird(Exception): ...

    body = protocol.error_body(Weird("boom with secret"))
    assert set(body) == {"kind", "message", "retry_after", "type"}
    assert body["type"] == "Weird" and body["message"] == "boom with secret" and body["kind"] is None

    seen = []
    raised = protocol.exception_from_body(body, lambda error: seen.append(error) or RuntimeError("host"))
    assert str(raised) == "host"
    assert seen == [protocol.RemoteError(None, "boom with secret", None, "Weird")]


def test_a_builtin_error_travels_with_the_kind_the_runner_gave_it():
    body = protocol.error_body(TimeoutError("timed out"))

    assert (body["kind"], body["retry_after"]) == ("timeout", None)
    assert "exception" not in body


def test_an_error_naming_an_unknown_contract_exception_is_a_protocol_error():
    body = {"kind": None, "message": "x", "retry_after": None, "exception": {"type": "SystemExit", "fields": {}}}

    with pytest.raises(ProtocolError):
        protocol.exception_from_body(body, fail)


# --- replies ----------------------------------------------------------------


def test_a_reply_is_read_only_as_far_as_the_caller_goes():
    consumed = []

    def reply():
        yield protocol.stream_frame()
        for index in range(3):
            consumed.append(index)
            yield protocol.item_frame([index])
        yield protocol.end_frame()

    stream = protocol.read_reply(reply(), fail)
    assert consumed == []
    assert next(stream) == [0]
    assert consumed == [0]
    assert list(stream) == [[1], [2]]


def test_an_error_after_a_stream_started_is_raised_at_the_point_of_iteration():
    def reply():
        yield protocol.stream_frame()
        yield protocol.item_frame([1])
        yield protocol.error_frame(ProviderRateLimited("quota", retry_after=2))

    stream = protocol.read_reply(reply(), fail)

    assert next(stream) == [1]
    with pytest.raises(ProviderRateLimited):
        next(stream)


def test_a_reply_that_ends_too_soon_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        protocol.read_reply(iter([]), fail)
    with pytest.raises(ProtocolError):
        list(protocol.read_reply(iter([protocol.stream_frame()]), fail))
    cut = [protocol.result_frame(StorageStreamResult(chunks=iter([])), chunks=True), protocol.bytes_frame(b"a")]
    with pytest.raises(ProtocolError):
        list(protocol.read_reply(iter(cut), fail).chunks)


def test_the_frames_of_a_reply_are_closed_once_it_has_been_read():
    class Closing:
        def __init__(self, items):
            self.items, self.closed = iter(items), False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.items)

        def close(self):
            self.closed = True

    unary = Closing([protocol.result_frame({"a": 1})])
    assert protocol.read_reply(unary, fail) == {"a": 1} and unary.closed

    failing = Closing([protocol.error_frame(PermissionError("x"))])
    with pytest.raises(PermissionError):
        protocol.read_reply(failing, fail)
    assert failing.closed

    stream = Closing([protocol.stream_frame(), protocol.item_frame(1), protocol.end_frame()])
    assert list(protocol.read_reply(stream, fail)) == [1] and stream.closed
