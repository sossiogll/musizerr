"""
The call protocol between the host and a provider's runner, standard library
only.

**Frames.** Everything that travels is a frame: a 4-byte big-endian length (of
what follows), one byte for the frame type, then the payload. ``FRAME_JSON``
carries a JSON message, ``FRAME_BYTES`` raw bytes (the chunks of a file the
provider streams). A frame is at most ``MAX_FRAME_SIZE`` long.

**Requests** (host to runner, one JSON frame)::

    {"v": 1, "sdk": "6.0.0", "op": "describe", "context": <ContextSnapshot>}
    {"v": 1, "sdk": "6.0.0", "op": "call", "method": "search_tracks", "args": [...], "kwargs": {...},
     "session": <AuthSession> | null, "session_at": <index in args> | null,
     "context": <ContextSnapshot> | null}

``args``, ``kwargs``, ``session`` and ``context`` are values of
``musizeer_sdk.wire``. The session is carried apart from the arguments and put
back where it was (``session_at``, an index in ``args``; ``null``: the keyword
``session``). ``context`` is the snapshot of the provider's settings, timeouts
and public URL prefix for THIS call: the provider never calls back into the
host (``CONTEXT_FREE_METHODS`` are called without one).

``sdk`` is the host's SDK version (ADR-0007 §3.1): the runner encodes its replies for it, leaving out
the fields newer than the host knows; a request without it is taken for the first release of the major (BASELINE). The host
encodes a call for the provider's own SDK version, which its manifest says.

**Replies** (runner to host, a sequence of frames)::

    {"t": "manifest", "value": <Manifest>}            then nothing
    {"t": "result", "value": ..., "chunks": false}    then nothing
    {"t": "result", "value": <StorageStreamResult>, "chunks": true}
        then FRAME_BYTES frames, then {"t": "end"}
    {"t": "stream"} then {"t": "item", "value": ...} ... then {"t": "end"}
    {"t": "error", "error": {...}}                    in place of any of them

An iterator a provider returns (``iter_saved_tracks``, ``list_files``) is a
stream, produced as the host consumes it: asking for the first page does not
make the provider produce the rest. An error after a stream has started is
the last frame, and the host raises it at the point of iteration.

**Errors.** ``error`` is ``{"kind", "message", "retry_after", "type",
"exception"?}``. ``kind`` is the runner's classification (``musizeer_sdk.failures``,
``null`` when the type says nothing), ``type`` the name of the provider's
exception class. The exceptions of the contract (``wire.register_exception``)
travel as themselves in ``exception`` and are raised again as the same
classes with the same fields; everything else reaches the host as ``kind``,
``message`` and ``retry_after`` only (never a traceback).
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Callable, Iterator, NamedTuple

from . import manifest as _manifest  # noqa: F401 — registers the Manifest wire type
from . import wire
from .auth import AuthSession
from .context import ContextSnapshot
from .failures import classify_exception
from .version import BASELINE, SDK_VERSION, peer_minor

PROTOCOL_VERSION = 1

FRAME_JSON = 1
FRAME_BYTES = 2
MAX_FRAME_SIZE = 64 * 1024 * 1024
_HEADER = struct.Struct(">IB")

OP_DESCRIBE = "describe"
OP_CALL = "call"

MSG_MANIFEST = "manifest"
MSG_RESULT = "result"
MSG_STREAM = "stream"
MSG_ITEM = "item"
MSG_END = "end"
MSG_ERROR = "error"

# Methods that are pure functions of their arguments: called without a context
# snapshot (a hot path of the host, and nothing in them reads a setting).
CONTEXT_FREE_METHODS = frozenset({"supports_identifier"})


class ProtocolError(Exception):
    """A frame or a message that does not follow the protocol."""


# ------------------------------------------------------------------
# Frames
# ------------------------------------------------------------------


class Frame(NamedTuple):
    kind: int
    payload: bytes


def pack_frame(frame: Frame) -> bytes:
    size = len(frame.payload) + 1
    if frame.kind not in (FRAME_JSON, FRAME_BYTES):
        raise ProtocolError(f"unknown frame type {frame.kind}")
    if size > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame of {size} bytes is over the {MAX_FRAME_SIZE} bytes limit")
    return _HEADER.pack(size, frame.kind) + frame.payload


def _read_exactly(read: Callable[[int], bytes], count: int, *, eof_ok: bool) -> bytes | None:
    chunks: list[bytes] = []
    missing = count
    while missing:
        data = read(missing)
        if not data:
            if eof_ok and missing == count:
                return None
            raise ProtocolError("the connection ended in the middle of a frame")
        chunks.append(data)
        missing -= len(data)
    return b"".join(chunks)


def read_frame(read: Callable[[int], bytes]) -> Frame | None:
    """The next frame from ``read(n)`` (a socket's ``recv``, a file's ``read``),
    or None at a clean end of the stream."""
    header = _read_exactly(read, _HEADER.size, eof_ok=True)
    if header is None:
        return None
    size, kind = _HEADER.unpack(header)
    if size < 1 or size > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame of {size} bytes")
    if kind not in (FRAME_JSON, FRAME_BYTES):
        raise ProtocolError(f"unknown frame type {kind}")
    payload = _read_exactly(read, size - 1, eof_ok=False) if size > 1 else b""
    return Frame(kind, payload or b"")


class FrameSocket:
    """Frames over a connected socket."""

    def __init__(self, sock: Any) -> None:
        self._sock = sock

    def send(self, frame: Frame) -> None:
        self._sock.sendall(pack_frame(frame))

    def receive(self) -> Frame | None:
        return read_frame(self._sock.recv)


def json_frame(message: dict) -> Frame:
    return Frame(FRAME_JSON, json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def bytes_frame(data: bytes) -> Frame:
    return Frame(FRAME_BYTES, bytes(data))


def load_message(frame: Frame) -> dict:
    if frame.kind != FRAME_JSON:
        raise ProtocolError("expected a JSON frame")
    try:
        message = json.loads(frame.payload)
    except ValueError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("a message is a JSON object")
    return message


# ------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------


def split_session(args: tuple | list, kwargs: dict) -> tuple[list, dict, AuthSession | None, int | None]:
    """(args, kwargs, session, session_at): the call's ``AuthSession`` taken
    out of its arguments, and where it was."""
    for index, value in enumerate(args):
        if isinstance(value, AuthSession):
            return [*args[:index], *args[index + 1:]], dict(kwargs), value, index
    session = kwargs.get("session")
    if isinstance(session, AuthSession):
        return list(args), {k: v for k, v in kwargs.items() if k != "session"}, session, None
    return list(args), dict(kwargs), None, None


def join_session(args: list, kwargs: dict, session: AuthSession | None, session_at: int | None) -> tuple[list, dict]:
    """The inverse of :func:`split_session`."""
    if session is None:
        return args, kwargs
    if session_at is None:
        return args, {**kwargs, "session": session}
    return [*args[:session_at], session, *args[session_at:]], kwargs


def describe_request(context: ContextSnapshot | None = None) -> Frame:
    # Before its manifest the provider's SDK is not known: the context goes as the oldest peer reads it.
    return json_frame({"v": PROTOCOL_VERSION, "sdk": SDK_VERSION, "op": OP_DESCRIBE, "context": wire.encode(context, BASELINE)})


def call_request(
    method: str, args: tuple | list, kwargs: dict, context: ContextSnapshot | None, peer: tuple[int, int] = BASELINE,
) -> Frame:
    """A call, encoded for a provider of SDK minor ``peer`` (its manifest's ``sdk_version``)."""
    args, kwargs, session, session_at = split_session(args, kwargs)
    return json_frame({
        "v": PROTOCOL_VERSION,
        "sdk": SDK_VERSION,
        "op": OP_CALL,
        "method": method,
        "args": wire.encode(args, peer),
        "kwargs": wire.encode(kwargs, peer),
        "session": wire.encode(session, peer),
        "session_at": session_at,
        "context": wire.encode(context, peer),
    })


def peer_of(message: dict) -> tuple[int, int]:
    """The SDK minor of the host that sent ``message`` (BASELINE when it said nothing)."""
    return peer_minor(message.get("sdk"))


def parse_request(frame: Frame) -> dict:
    message = load_message(frame)
    if message.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {message.get('v')!r}, this runner speaks {PROTOCOL_VERSION}")
    if message.get("op") not in (OP_DESCRIBE, OP_CALL):
        raise ProtocolError(f"unknown operation {message.get('op')!r}")
    return message


# ------------------------------------------------------------------
# Replies, runner side
# ------------------------------------------------------------------


def manifest_frame(manifest: Any, peer: tuple[int, int] = BASELINE) -> Frame:
    return json_frame({"t": MSG_MANIFEST, "value": wire.encode(manifest, peer)})


def result_frame(value: Any, *, chunks: bool = False, peer: tuple[int, int] = BASELINE) -> Frame:
    return json_frame({"t": MSG_RESULT, "value": wire.encode(value, peer), "chunks": chunks})


def stream_frame() -> Frame:
    return json_frame({"t": MSG_STREAM})


def item_frame(value: Any, peer: tuple[int, int] = BASELINE) -> Frame:
    return json_frame({"t": MSG_ITEM, "value": wire.encode(value, peer)})


def end_frame() -> Frame:
    return json_frame({"t": MSG_END})


# ------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------


def error_body(exc: BaseException, peer: tuple[int, int] = BASELINE) -> dict[str, Any]:
    """The ``error`` of a reply for ``exc``."""
    failure = classify_exception(exc)
    body: dict[str, Any] = {
        "kind": failure.kind,
        "message": failure.message,
        "retry_after": failure.retry_after,
        "type": type(exc).__name__,
    }
    if wire.exception_type(exc) is not None:
        body["exception"] = wire.encode(exc, peer)
    return body


def error_frame(exc: BaseException, peer: tuple[int, int] = BASELINE) -> Frame:
    return json_frame({"t": MSG_ERROR, "error": error_body(exc, peer)})


@dataclass(frozen=True)
class RemoteError:
    """A failure of a call that is not an exception of the contract."""

    kind: str | None
    message: str
    retry_after: float | None = None
    type_name: str = ""


def exception_from_body(body: dict[str, Any], fail: Callable[[RemoteError], BaseException]) -> BaseException:
    """The exception to raise for the ``error`` of a reply: the contract's own
    class, or what ``fail`` makes of the rest."""
    try:
        contract = body.get("exception")
        if contract is None:
            remote = RemoteError(body["kind"], body["message"], body.get("retry_after"), body.get("type", ""))
        else:
            raised = wire.decode(contract)
            if not isinstance(raised, BaseException):
                raise ProtocolError("'exception' is not an exception")
            return raised
    except (KeyError, TypeError, wire.WireError) as exc:
        raise ProtocolError(f"malformed error: {exc!r}") from exc
    return fail(remote)


# ------------------------------------------------------------------
# Replies, host side
# ------------------------------------------------------------------


def _close(frames: Iterator[Frame]) -> None:
    close = getattr(frames, "close", None)
    if close is not None:
        close()


def _next_message(frames: Iterator[Frame]) -> dict:
    frame = next(frames, None)
    if frame is None:
        raise ProtocolError("the provider ended the reply before it was complete")
    return load_message(frame)


def _item_stream(frames: Iterator[Frame], fail: Callable[[RemoteError], BaseException]) -> Iterator[Any]:
    try:
        while True:
            message = _next_message(frames)
            kind = message.get("t")
            if kind == MSG_ITEM:
                yield wire.decode(message["value"])
            elif kind == MSG_END:
                return
            elif kind == MSG_ERROR:
                raise exception_from_body(message["error"], fail)
            else:
                raise ProtocolError(f"unexpected {kind!r} in a stream")
    finally:
        _close(frames)


def _byte_stream(frames: Iterator[Frame], fail: Callable[[RemoteError], BaseException]) -> Iterator[bytes]:
    try:
        for frame in frames:
            if frame.kind == FRAME_BYTES:
                yield frame.payload
                continue
            message = load_message(frame)
            kind = message.get("t")
            if kind == MSG_END:
                return
            if kind == MSG_ERROR:
                raise exception_from_body(message["error"], fail)
            raise ProtocolError(f"unexpected {kind!r} in a byte stream")
        raise ProtocolError("the provider ended the byte stream before its end")
    finally:
        _close(frames)


def read_reply(frames: Iterator[Frame], fail: Callable[[RemoteError], BaseException]) -> Any:
    """What the reply in ``frames`` carries: the manifest or the result (with
    its byte stream, lazily), a lazy iterator for a stream, or the exception
    for an error. The frames are read only as far as the caller consumes
    the stream."""
    try:
        message = _next_message(frames)
        kind = message.get("t")
        if kind == MSG_MANIFEST:
            value = wire.decode(message["value"])
        elif kind == MSG_RESULT and message.get("chunks"):
            return wire.decode(message["value"], chunks=_byte_stream(frames, fail))
        elif kind == MSG_RESULT:
            value = wire.decode(message["value"])
        elif kind == MSG_STREAM:
            return _item_stream(frames, fail)
        elif kind == MSG_ERROR:
            raise exception_from_body(message["error"], fail)
        else:
            raise ProtocolError(f"unexpected {kind!r} at the start of a reply")
    except BaseException:
        _close(frames)
        raise
    _close(frames)
    return value
