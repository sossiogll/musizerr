"""
The wire codec: a lossless, JSON-compatible encoding of every value that
crosses the provider contract in either direction (arguments, results, the
fields of an exception, the manifest), standard library only.

``core/serialization.py`` of the host is the API view (it adds ``kind``, drops
what the API does not show); this module is the opposite: ``decode(encode(x))
== x`` for every contract type, and the encoded form survives
``json.dumps`` / ``json.loads``.

What is plain JSON stays plain: ``None``, ``bool``, ``int``, finite ``float``,
``str``, ``list`` and a ``dict`` whose keys are all strings. Anything else
carries an explicit tag, the ``"$"`` key:

- the registered dataclasses (``{"$": "Track", "title": ..., ...}``, one key
  per field) and enums (``{"$": "enum", "e": "AuthFlow", "v": "oauth2"}``);
- ``tuple``, ``set``, ``frozenset`` and ``bytes``, because the contract cares
  about them (a ``tuple`` field must not come back as a ``list``);
- a ``datetime`` (``isoformat``: naive stays naive, aware keeps its offset);
- a ``dict`` with a non-string key, or with a ``"$"`` key of its own (a list of
  ``[key, value]`` pairs), and a non-finite ``float``.

The exceptions of the contract (``register_exception`` below) are values too: one
that travels as itself is a tagged object with the arguments it is built from,
and ``DelegateHoldsRelease`` is also an argument (``search_delegated``).

An unknown tag, an unregistered dataclass or enum and a value of any other type
are a :class:`WireError`: nothing is guessed.

**Evolution within a contract major** (ADR-0007 §3.1). Decoding ignores a field
it does not know (a newer peer's) and gives a missing one its default (an older
peer's). A field added after the first release of the major carries the minor
it exists since, ``field(default=..., metadata={SINCE: "5.5"})``, and must have a
default; ``encode(value, peer=(5, 4))`` leaves out the fields newer than the
peer's minor, so a peer that still decodes strictly (every SDK before 5.5) never
meets one. ``peer=None`` encodes everything. An iterator is not a value; the
protocol streams it (``musizerr_sdk.protocol``). The one iterator inside a
contract type, ``StorageStreamResult.chunks``, is encoded as a marker and
rebuilt from the byte stream the caller hands to :func:`decode`.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterator, NamedTuple, TypeVar

from . import auth, context, delegation, errors, identifiers, interfaces, models, permissions, settings
from .version import minor_of

TAG = "$"
# The metadata key of a field added after the first release of the contract major: the SDK minor it
# exists since ("5.5").
SINCE = "since"

Peer = tuple[int, int] | None

_T = TypeVar("_T", bound=type)


class WireError(ValueError):
    """A value the codec cannot encode, or data it cannot decode."""


_classes: dict[str, type] = {}
_fields: dict[type, tuple[str, ...]] = {}
_known: dict[type, frozenset[str]] = {}
# The fields a type gained after the first release of the major, with the minor of each.
_since: dict[type, tuple[tuple[str, tuple[int, int]], ...]] = {}
_fields_for_peer: dict[tuple[type, tuple[int, int]], tuple[str, ...]] = {}
_enums: dict[str, type[Enum]] = {}
_enum_names: dict[type, str] = {}

# Dataclass fields that hold an iterator: encoded as a marker (the bytes travel
# as frames of their own), rebuilt from the stream given to ``decode``.
_STREAM_FIELDS: dict[type, frozenset[str]] = {interfaces.StorageStreamResult: frozenset({"chunks"})}

# Types that are their own encoding (a float is too, when it is finite): the hot loops
# below do not make a call for each of them.
_PLAIN = frozenset({str, int, bool})
_PLAIN_JSON = _PLAIN | {float}


def register(cls: _T) -> _T:
    """Make a dataclass a type of the wire. Usable as a decorator."""
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise TypeError(f"{cls!r} is not a dataclass")
    name = cls.__name__
    if _classes.get(name, cls) is not cls:
        raise ValueError(f"two wire types are named {name}")
    fields = [f for f in dataclasses.fields(cls) if f.init]
    marked = []
    for f in fields:
        if SINCE not in f.metadata:
            continue
        minor = minor_of(f.metadata[SINCE])
        if minor is None:
            raise TypeError(f"{name}.{f.name}: '{f.metadata[SINCE]}' is not an SDK minor")
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise TypeError(f"{name}.{f.name} was added in {f.metadata[SINCE]}: it needs a default (an older peer never sends it)")
        marked.append((f.name, minor))
    _classes[name] = cls
    _fields[cls] = tuple(f.name for f in fields)
    _known[cls] = frozenset(_fields[cls])
    if marked:
        _since[cls] = tuple(marked)
    return cls


def _peer_fields(kind: type, peer: tuple[int, int]) -> tuple[str, ...]:
    """The fields of ``kind`` a peer of minor ``peer`` knows."""
    key = (kind, peer)
    fields = _fields_for_peer.get(key)
    if fields is None:
        newer = {name for name, minor in _since[kind] if minor > peer}
        fields = _fields_for_peer[key] = tuple(name for name in _fields[kind] if name not in newer)
    return fields


def register_enum(cls: _T) -> _T:
    """Make an enum a type of the wire. Usable as a decorator."""
    if not (isinstance(cls, type) and issubclass(cls, Enum)):
        raise TypeError(f"{cls!r} is not an enum")
    if _enums.get(cls.__name__, cls) is not cls:
        raise ValueError(f"two wire enums are named {cls.__name__}")
    _enums[cls.__name__] = cls
    _enum_names[cls] = cls.__name__
    return cls


class _ExceptionType(NamedTuple):
    tag: str
    fields: Callable[[Any], dict[str, Any]]
    build: Callable[[dict[str, Any]], BaseException]


_exceptions: dict[type, _ExceptionType] = {}
_exception_tags: dict[str, _ExceptionType] = {}


def register_exception(
    cls: type[BaseException], fields: Callable[[Any], dict[str, Any]], build: Callable[[dict[str, Any]], BaseException],
) -> None:
    """Make an exception a value of the wire: ``fields`` gives the arguments it is
    built from (wire values), ``build`` makes it again from them. A subclass is
    sent as its nearest registered ancestor."""
    entry = _ExceptionType(cls.__name__, fields, build)
    if _exception_tags.get(entry.tag, entry) is not entry:
        raise ValueError(f"two wire exceptions are named {entry.tag}")
    _exceptions[cls] = entry
    _exception_tags[entry.tag] = entry


def exception_type(exc: BaseException) -> str | None:
    """The tag of the registered exception ``exc`` travels as, or None."""
    for cls in type(exc).__mro__:
        if cls in _exceptions:
            return _exceptions[cls].tag
    return None


def registered_exceptions() -> dict[str, type]:
    return {entry.tag: cls for cls, entry in _exceptions.items()}


def registered_types() -> dict[str, type]:
    """The dataclasses the wire knows, by tag."""
    return dict(_classes)


def registered_enums() -> dict[str, type[Enum]]:
    return dict(_enums)


def _register_module(module: Any) -> None:
    for obj in vars(module).values():
        if isinstance(obj, type) and obj.__module__ == module.__name__:
            if dataclasses.is_dataclass(obj):
                register(obj)
            elif issubclass(obj, Enum):
                register_enum(obj)


for _module in (models, identifiers, auth, delegation, settings, permissions, interfaces, context):
    _register_module(_module)


# The exceptions of the contract: the ones the host acts on by type, which cross as
# themselves (a subclass as its nearest ancestor here).
register_exception(
    errors.DownloadDelegated,
    lambda e: {"provider": e.provider, "details": e.details, "handle": e.handle},
    lambda f: errors.DownloadDelegated(f["provider"], f["details"], f["handle"]),
)
register_exception(
    errors.DownloadDeclined,
    lambda e: {"provider": e.provider, "code": e.code, "message": e.message},
    lambda f: errors.DownloadDeclined(f["provider"], f["code"], f["message"]),
)
register_exception(
    errors.DelegateHoldsRelease,
    lambda e: {
        "provider": e.provider, "handle": e.handle, "tracks": e.tracks, "complete": e.complete,
        "release": e.release, "details": e.details, "state": e.state,
    },
    lambda f: errors.DelegateHoldsRelease(
        f["provider"], f["handle"], f["tracks"], complete=f["complete"], release=f["release"],
        details=f["details"], state=f["state"],
    ),
)
register_exception(
    errors.RemotePlaylistGone, lambda e: {"message": str(e)}, lambda f: errors.RemotePlaylistGone(f["message"]),
)
register_exception(
    errors.ProviderRateLimited,
    lambda e: {"message": str(e), "retry_after": e.retry_after},
    lambda f: errors.ProviderRateLimited(f["message"], retry_after=f["retry_after"]),
)
register_exception(PermissionError, lambda e: {"message": str(e)}, lambda f: PermissionError(f["message"]))
register_exception(NotImplementedError, lambda e: {"message": str(e)}, lambda f: NotImplementedError(f["message"]))


# ------------------------------------------------------------------
# Encoding
# ------------------------------------------------------------------


def encode(value: Any, peer: Peer = None) -> Any:
    """``value`` as JSON-compatible data, for a peer of SDK minor ``peer`` (None: every field). Raises
    :class:`WireError`."""
    if value is None:
        return None
    kind = type(value)
    if kind is str or kind is bool or kind is int:
        return value
    if kind is float:
        return value if math.isfinite(value) else {TAG: "float", "v": repr(value)}
    if kind is list:
        return [encode(item, peer) for item in value]
    if kind is dict:
        return _encode_dict(value, peer)
    fields = _fields.get(kind)
    if fields is not None:
        if peer is not None and kind in _since:
            fields = _peer_fields(kind, peer)
        return _encode_object(value, kind, fields, peer)
    if kind is tuple:
        return {TAG: "tuple", "v": [encode(item, peer) for item in value]}
    return _encode_other(value, kind, peer)


def _encode_dict(value: Mapping, peer: Peer) -> Any:
    if TAG not in value and all(type(key) is str for key in value):
        return {key: encode(item, peer) for key, item in value.items()}
    return {TAG: "dict", "v": [[encode(key, peer), encode(item, peer)] for key, item in value.items()]}


def _encode_object(value: Any, kind: type, fields: tuple[str, ...], peer: Peer) -> dict:
    out: dict[str, Any] = {TAG: kind.__name__}
    streams = _STREAM_FIELDS.get(kind, ())
    for name in fields:
        item = getattr(value, name)
        if item is None or type(item) in _PLAIN:
            out[name] = item
        elif name in streams:
            out[name] = {TAG: "chunks"}
        else:
            out[name] = encode(item, peer)
    return out


def _encode_other(value: Any, kind: type, peer: Peer) -> Any:
    if kind in _enum_names:
        return {TAG: "enum", "e": _enum_names[kind], "v": value.value}
    if isinstance(value, Enum):
        raise WireError(f"the enum {kind.__name__} is not a wire type")
    if isinstance(value, BaseException):
        for cls in kind.__mro__:
            if cls in _exceptions:
                entry = _exceptions[cls]
                return {TAG: entry.tag, **{name: encode(item, peer) for name, item in entry.fields(value).items()}}
        raise WireError(f"the exception {kind.__name__} is not a wire type")
    if isinstance(value, datetime):
        return {TAG: "datetime", "v": value.isoformat()}
    if isinstance(value, (set, frozenset)):
        # Sorted by their encoding, so that the same set is always the same bytes.
        items = sorted((encode(item, peer) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
        return {TAG: "set" if isinstance(value, set) else "frozenset", "v": items}
    if isinstance(value, (bytes, bytearray)):
        return {TAG: "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return encode(float(value), peer)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Mapping):
        return _encode_dict(value, peer)
    if isinstance(value, list):
        return [encode(item, peer) for item in value]
    if isinstance(value, tuple):
        return {TAG: "tuple", "v": [encode(item, peer) for item in value]}
    raise WireError(f"{kind.__module__}.{kind.__qualname__} is not a wire type")


# ------------------------------------------------------------------
# Decoding
# ------------------------------------------------------------------


def decode(data: Any, *, chunks: Iterator[bytes] | None = None) -> Any:
    """The value ``data`` encodes. ``chunks`` is the byte stream a
    ``StorageStreamResult`` marker is rebuilt with. Raises :class:`WireError`."""
    return _decode(data, chunks)


def _decode(data: Any, chunks: Iterator[bytes] | None) -> Any:
    kind = type(data)
    if data is None or kind is str or kind is int or kind is bool or kind is float:
        return data
    if kind is list:
        return [_decode(item, chunks) for item in data]
    if kind is not dict:
        raise WireError(f"{kind.__name__} is not JSON data")
    if TAG not in data:
        return {key: _decode(item, chunks) for key, item in data.items()}
    tag = data[TAG]
    cls = _classes.get(tag) if type(tag) is str else None
    if cls is not None:
        return _decode_object(cls, data, chunks)
    return _decode_tagged(tag, data, chunks)


def _decode_object(cls: type, data: dict, chunks: Iterator[bytes] | None) -> Any:
    streams = _STREAM_FIELDS.get(cls, ())
    known = _known[cls]
    kwargs = {}
    for name, item in data.items():
        if name not in known:  # the tag, or a field of a newer peer's SDK: ignored
            continue
        if name in streams and item is not None:
            if item != {TAG: "chunks"}:
                raise WireError(f"{cls.__name__}.{name} is not a byte stream marker")
            if chunks is None:
                raise WireError(f"{cls.__name__}.{name} is a byte stream and none was given")
            kwargs[name] = chunks
        elif item is None or type(item) in _PLAIN_JSON:
            kwargs[name] = item
        else:
            kwargs[name] = _decode(item, chunks)
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise WireError(f"cannot build a {cls.__name__}: {exc}") from exc


def _decode_tagged(tag: Any, data: dict, chunks: Iterator[bytes] | None) -> Any:
    entry = _exception_tags.get(tag) if type(tag) is str else None
    if entry is not None:
        try:
            return entry.build({name: _decode(item, chunks) for name, item in data.items() if name != TAG})
        except (KeyError, TypeError, ValueError) as exc:
            raise WireError(f"cannot build a {tag}: {exc!r}") from exc
    try:
        if tag == "tuple":
            return tuple(_decode(item, chunks) for item in data["v"])
        if tag == "set":
            return {_decode(item, chunks) for item in data["v"]}
        if tag == "frozenset":
            return frozenset(_decode(item, chunks) for item in data["v"])
        if tag == "dict":
            return {_decode(key, chunks): _decode(item, chunks) for key, item in data["v"]}
        if tag == "datetime":
            return datetime.fromisoformat(data["v"])
        if tag == "bytes":
            return base64.b64decode(data["v"], validate=True)
        if tag == "float":
            return float(data["v"])
        if tag == "enum":
            return _enums[data["e"]](data["v"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"malformed {tag!r} value: {exc!r}") from exc
    raise WireError(f"unknown wire tag {tag!r}")


# ------------------------------------------------------------------
# JSON text
# ------------------------------------------------------------------


def dumps(value: Any, peer: Peer = None) -> bytes:
    """``value`` as compact UTF-8 JSON (for a peer of SDK minor ``peer``)."""
    return json.dumps(encode(value, peer), separators=(",", ":"), allow_nan=False).encode("utf-8")


def loads(payload: bytes | str, *, chunks: Iterator[bytes] | None = None) -> Any:
    try:
        return decode(json.loads(payload), chunks=chunks)
    except json.JSONDecodeError as exc:
        raise WireError(f"not JSON: {exc}") from exc

