"""
Every provider is reached through the call protocol: a proxy built from the
manifest, a transport, a runner that holds the provider. The loopback transport
runs the runner in this process, and nothing is passed by reference.

These tests pin the host side of the protocol: each method of the contract
survives the round trip, the declared capabilities are the manifest's and cost
no call, a failure arrives classified, a stream is lazy, a setting changed
between two calls is seen by the second, and the host holds no provider.
"""
from __future__ import annotations

import inspect
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
import pytest

from core.interfaces import metadata_capabilities, personal_capabilities
from musizeer_sdk import manifest as manifests, protocol, wire
from musizeer_sdk.auth import (
    AccountIdentity, AccountModel, AuthDefinition, AuthFlow, AuthSession, BearerHeaderRequestAuth, OAuth2Config,
)
from musizeer_sdk.context import ContextSnapshot, StaticProviderContext
from musizeer_sdk.delegation import DOWNLOADING, DelegationStatus, FileRef
from musizeer_sdk.errors import (
    DelegateHoldsRelease, DownloadDeclined, DownloadDelegated, ProviderRateLimited, RemotePlaylistGone,
)
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.interfaces import (
    BaseProvider, DownloadProvider, Identity, IdentificationProvider, LyricsProvider, MetadataProvider,
    StorageProvider, StorageStreamResult, SyncCandidate, SyncProvider, WorkspaceFile,
)
from musizeer_sdk.models import Album, Artist, LibraryItem, Lyrics, Playlist, Track
from services import errors as host_errors
from services.provider_context import HostProviderContext
from services.provider_proxy import (
    ProviderCallFailed, ProviderProxy, Transport, TransportError, connect, proxy_class,
)
from services.provider_registry import ProviderRegistry
from musizeer_sdk.runner import ProviderRunner

from tests import host_boundary
from tests.conftest import FakeAuthManager, FakeMetadataProvider, FakeRegistry, FakeSyncProvider, make_track
from tests.loopback import HostContextWithoutDatabase, LoopbackTransport, as_proxy, host_proxy, loopback_proxy

NO_AUTH = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
SESSION = AuthSession(flow=AuthFlow.OAUTH2, headers={"Authorization": "Bearer t"}, params={"k": 1}, extra={"a": [1]})
TRACK = make_track("Song", "Artist", "Album", isrc="ISRC1", provider="p", provider_id="1")
ALBUM = Album(title="Album", artist="Artist", year=2001, tracks=[TRACK], external_ids=[Identifier("mbid", "m")])
ARTIST = Artist(name="Artist", provider="p", provider_id="a")
PLAYLIST = Playlist(name="List", provider="p", provider_id="l", tracks=[TRACK])
SYNC_ID_TRACK = make_track("Synced", provider_id="s1")


# ------------------------------------------------------------------
# Conformance: every method of the contract survives the round trip
# ------------------------------------------------------------------


@dataclass
class Sample:
    """One call: the arguments the host sends (without the session unless it is
    part of them), and what the provider answers."""

    args: tuple
    result: Any = None
    kwargs: dict = field(default_factory=dict)
    stream: bool = False  # the provider answers with an iterator


WS = WorkspaceFile(workspace="/ws", name="a.flac")
HELD = DelegateHoldsRelease("p", "h", [TRACK], complete=False, release="R", details=[{"d": 1}], state={"s": 1})
ID_ISRC = Identifier("isrc", "ISRC1")

SAMPLES: dict[str, Sample] = {
    # BaseProvider
    "validate_config": Sample(({"url": "u", "n": 1},), True),
    "test_connection": Sample((SESSION,), "refused"),
    "identify_account": Sample((SESSION,), AccountIdentity("id-1", "Alice")),
    "get_cover_url": Sample(("c1", SESSION), "https://cdn/c1.jpg"),
    # MetadataProvider
    "search_tracks": Sample(("q", SESSION, 5), [TRACK]),
    "search_albums": Sample(("q", SESSION, 5), [ALBUM]),
    "search_artists": Sample(("q", SESSION, 5), [ARTIST]),
    "search_playlists": Sample(("q", SESSION, 5), [PLAYLIST]),
    "get_track": Sample(("1", SESSION), TRACK),
    "get_preview_url": Sample(("1", SESSION), "https://p/1.mp3"),
    "find_by_isrc": Sample(("ISRC1", SESSION), TRACK),
    "find_related_tracks": Sample((TRACK, SESSION, 3), [TRACK]),
    "find_similar_tracks": Sample((TRACK, SESSION, 3), [TRACK]),
    "get_album": Sample(("a", SESSION), ALBUM),
    "get_artist": Sample(("a", SESSION), ARTIST),
    "get_artist_albums": Sample(("a", SESSION, 100), [ALBUM]),
    "get_artist_top_tracks": Sample(("a", SESSION, 10), [TRACK]),
    "get_playlist": Sample(("l", SESSION), PLAYLIST),
    "get_playlist_snapshot": Sample(("l", SESSION), "snap-1"),
    "get_new_releases": Sample((SESSION, 5), [ALBUM]),
    "get_trending_albums": Sample((SESSION, 5), [ALBUM]),
    "get_trending_tracks": Sample((SESSION, 5), [TRACK]),
    "get_trending_artists": Sample((SESSION, 5), [ARTIST]),
    "get_saved_tracks": Sample((SESSION, 50, 100), [TRACK]),
    "iter_saved_tracks": Sample((SESSION, 2), [[TRACK], [TRACK, TRACK]], stream=True),
    "get_saved_albums": Sample((SESSION, 50, 0), [ALBUM]),
    "get_followed_artists": Sample((SESSION, 50, 0), [ARTIST]),
    "get_saved_playlists": Sample((SESSION, 50, 0), [PLAYLIST]),
    "unlike_saved_track": Sample(("1", SESSION), None),
    # LyricsProvider
    "get_lyrics": Sample((TRACK, SESSION), Lyrics(plain="la", source="x")),
    # DownloadProvider
    "supports_identifier": Sample((ID_ISRC,), True),
    "download_by_id": Sample((ID_ISRC, "/ws", SESSION), "song.ogg"),
    "download_fuzzy": Sample((TRACK, "/ws", SESSION), "song.ogg"),
    "dispatch_album": Sample((ALBUM, SESSION), None),
    "search_delegated": Sample((HELD, SESSION), None),
    "poll_delegation": Sample(("h", SESSION), DelegationStatus(state=DOWNLOADING, detail="d")),
    "list_delegated_tracks": Sample(("h", SESSION), [TRACK]),
    "list_delegated_files": Sample(("h", SESSION), [FileRef("/f.flac", True, 1000, 44100, "x")]),
    "delete_file": Sample((FileRef("/f.flac"), SESSION), True),
    "request_rescan": Sample((SESSION,), None),
    "delegation_event_handle": Sample(({"event": "done", "job_id": 7, "nested": {"a": [1, None]}},), "7"),
    "register_push_endpoint": Sample(("https://host/hook", SESSION), "created"),
    # StorageProvider
    "store": Sample((WS, "A/b.flac", SESSION), "A/b.flac"),
    "exists": Sample(("A/b.flac", SESSION), True),
    "stream": Sample(("A/b.flac", "/ws", SESSION), StorageStreamResult(
        file=None, url=None, headers={"h": "v"}, chunks=iter([b"ab", b"", b"\x00\xff"]), media_type="audio/flac",
        filename="b.flac", size=4, offset=0,
    ), kwargs={"offset": 0}),
    "delete": Sample(("A/b.flac", SESSION), True),
    "size": Sample(("A/b.flac", SESSION), 4),
    "list_files": Sample(("A/", SESSION), ["A/b.flac", "A/c.flac"], stream=True),
    # SyncProvider
    "trigger_scan": Sample((SESSION,), None),
    "find_candidates": Sample((TRACK, SESSION), [SyncCandidate("s1", SYNC_ID_TRACK)]),
    "get_stream_url": Sample(("s1", SESSION), "http://sync/s1"),
    "like_track": Sample(("s1", SESSION), None),
    "unlike_track": Sample(("s1", SESSION), None),
    "create_playlist": Sample(("Drive", ["s1", "s2"], SESSION), "remote-1"),
    "update_playlist": Sample(("remote-1", "Drive", ["s2"], SESSION), None),
    "delete_playlist": Sample(("remote-1", SESSION), None),
    "list_library": Sample((SESSION,), [LibraryItem(sync_id="s1", path="/a", title="T", artist="A", size=3)]),
    "fetch_partial": Sample(("s1", "/ws", SESSION, 1000), "partial"),
    # IdentificationProvider
    "is_available": Sample((), True),
    "identify": Sample((WS,), Identity(title="T", artist="A", score=0.9, recording_id="rec")),
}

ROLE_ABCS = [BaseProvider, MetadataProvider, DownloadProvider, LyricsProvider, StorageProvider, SyncProvider,
             IdentificationProvider]


def _calls() -> set[str]:
    """The methods of the contract that are calls (not answered from the manifest)."""
    declared = {name for names in manifests.DECLARED_QUERIES.values() for name in names}
    return {
        name
        for abc in ROLE_ABCS for name, member in vars(abc).items()
        if not name.startswith("_") and inspect.isfunction(member) and name not in declared and name != "config_schema"
    }


def test_every_method_of_the_contract_has_a_round_trip_sample():
    assert _calls() == set(SAMPLES), (
        f"missing: {sorted(_calls() - set(SAMPLES))}, stale: {sorted(set(SAMPLES) - _calls())}"
    )


def test_every_sample_is_made_of_wire_types():
    for name, sample in SAMPLES.items():
        for value in (*sample.args, sample.kwargs, sample.result):
            if isinstance(value, StorageStreamResult):
                value = StorageStreamResult(file=value.file, url=value.url, headers=value.headers, size=value.size)
            assert wire.dumps(wire.loads(wire.dumps(value))) == wire.dumps(value), name


def _implement(name: str):
    def method(self, *args, **kwargs):
        self.received.append((name, args, kwargs))
        sample = SAMPLES[name]
        result = sample.result
        if isinstance(result, StorageStreamResult):
            return StorageStreamResult(
                file=result.file, url=result.url, headers=result.headers, chunks=iter([b"ab", b"", b"\x00\xff"]),
                media_type=result.media_type, filename=result.filename, size=result.size, offset=result.offset,
            )
        return iter(result) if sample.stream else result

    method.__name__ = name
    return method


def _everything_init(self, context) -> None:
    BaseProvider.__init__(self, context)
    self.received = []


Everything = type(
    "Everything",
    (MetadataProvider, DownloadProvider, LyricsProvider, StorageProvider, SyncProvider, IdentificationProvider),
    {
        "__init__": _everything_init,
        "name": "everything", "label": "Everything", "auth": NO_AUTH,
        "supported_identifier_types": {"isrc"}, "supports_fuzzy": True, "supports_stream_offset": True,
        "streams_full_tracks": True, "max_concurrent_downloads": 2,
        **{name: _implement(name) for name in SAMPLES},
    },
)


@pytest.fixture
def everything():
    holder: list[Everything] = []

    def factory(context):
        holder.append(Everything(context))
        return holder[0]

    proxy = loopback_proxy(factory, StaticProviderContext(provider_name="everything"))
    return proxy, holder[0]


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_a_method_of_the_contract_goes_through_the_protocol_and_comes_back_equal(everything, name):
    proxy, provider = everything
    sample = SAMPLES[name]

    answer = getattr(proxy, name)(*sample.args, **sample.kwargs)

    received_name, received_args, received_kwargs = provider.received[-1]
    assert received_name == name
    assert wire.dumps([received_args, received_kwargs]) == wire.dumps([sample.args, sample.kwargs])
    if sample.stream:
        assert not isinstance(answer, list) and list(answer) == sample.result
    elif isinstance(sample.result, StorageStreamResult):
        assert (answer.headers, answer.media_type, answer.filename, answer.size) == (
            sample.result.headers, sample.result.media_type, sample.result.filename, sample.result.size,
        )
        assert list(answer.chunks) == [b"ab", b"", b"\x00\xff"]
    else:
        assert answer == sample.result


def test_a_proxy_is_an_instance_of_every_role_of_its_manifest(everything):
    proxy, _ = everything

    for abc in ROLE_ABCS:
        assert isinstance(proxy, abc)
    assert proxy.roles == frozenset(manifests.ROLES)
    assert not inspect.isabstract(type(proxy))


# ------------------------------------------------------------------
# Declared, never inferred: the manifest answers, with no call
# ------------------------------------------------------------------


class Counting:
    """A transport that counts the requests it carries."""

    def __init__(self, inner: Transport) -> None:
        self.inner, self.requests = inner, []

    def request(self, request):
        self.requests.append(protocol.parse_request(request))
        return self.inner.request(request)


class SnapshotCounter(StaticProviderContext):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return super().snapshot()


def counted(factory, context=None):
    transport = Counting(LoopbackTransport(ProviderRunner(factory)))
    return connect(transport, context or StaticProviderContext()), transport


def test_the_declarations_are_the_manifests_and_cost_no_call():
    proxy, transport = counted(Everything)
    described = len(transport.requests)

    answers = (
        proxy.supports_login(), proxy.is_library_available(), proxy.supports_discography(), proxy.supports_user_library(),
        proxy.supports_unlike_saved_track(), proxy.restricts_playlist_tracks(), proxy.supports_isrc_lookup(),
        proxy.supports_likes(), proxy.supports_playlists(), proxy.streams_full_tracks, proxy.supported_identifier_types,
        proxy.max_concurrent_downloads, proxy.supports_fuzzy, proxy.enabled_by_default, proxy.supports_stream_offset,
        proxy.config_schema(), proxy.name, proxy.label, proxy.auth, proxy.implements("get_track"),
    )

    assert len(transport.requests) == described == 1, "only the describe went over the transport"
    assert answers[:9] == (False, None, False, False, False, False, False, False, False)
    assert answers[9:15] == (True, frozenset({"isrc"}), 2, True, True, True)
    assert proxy.supported_identifier_types == {"isrc"}


def test_a_pure_query_carries_no_context_snapshot_and_a_call_carries_one():
    context = SnapshotCounter()
    proxy, transport = counted(Everything, context)

    proxy.supports_identifier(ID_ISRC)
    assert context.snapshots == 0 and transport.requests[-1]["context"] is None
    proxy.get_track("1", SESSION)
    assert context.snapshots == 1 and transport.requests[-1]["context"] is not None


def test_the_roles_are_the_manifests_and_a_provider_of_two_is_filed_under_both():
    registry = ProviderRegistry(discover=False)
    registry.register(loopback_proxy(Streaming, StaticProviderContext(provider_name="streaming")))
    registry.register(as_proxy(FakeSyncProvider("only-sync")))

    assert registry.storage_names() == ["streaming"] and registry.metadata_names() == ["streaming"]
    assert registry.get_storage_provider("streaming") is registry.get_metadata_provider("streaming")
    assert registry.get_storage_provider("streaming").roles == frozenset({"storage", "metadata"})
    # A provider of one role is in no other.
    assert registry.sync_names() == ["only-sync"] and registry.get_metadata_provider("only-sync") is None
    assert registry.get_sync_provider("only-sync").roles == frozenset({"sync"})


def test_what_a_provider_implements_is_the_manifests():
    fake = FakeMetadataProvider("m")
    proxy = as_proxy(fake)

    assert proxy.implements("search_tracks") and not proxy.implements("search_playlists")
    assert proxy.implements("validate_config")


def test_a_search_does_not_ask_a_provider_that_does_not_implement_it():
    from core.search_aggregator import SearchAggregator

    class Playlists(FakeMetadataProvider):
        def search_playlists(self, query, session, limit=20):
            return [PLAYLIST]

    transports = {}
    for provider in (FakeMetadataProvider("plain", tracks=[TRACK]), Playlists("listing")):
        transports[provider.name] = counted(lambda _context, built=provider: built)
    registry = FakeRegistry(metadata=[proxy for proxy, _ in transports.values()])

    found = SearchAggregator(registry, FakeAuthManager(), ["plain", "listing"]).search_playlists("q")

    assert [p.name for p in found] == [PLAYLIST.name]
    asked = {
        name: [r["method"] for r in transport.requests if r["op"] == "call"] for name, (_proxy, transport) in transports.items()
    }
    assert asked == {"plain": [], "listing": ["search_playlists"]}


# ------------------------------------------------------------------
# Failures: classified where the exception types are, raised again by kind
# ------------------------------------------------------------------


def _status(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example/x")
    return httpx.HTTPStatusError(f"{code}", request=request, response=httpx.Response(code, request=request, headers=headers))


class Failing(LyricsProvider):
    name, label, auth = "failing", "Failing", NO_AUTH

    def __init__(self, context) -> None:
        super().__init__(context)
        self.error: BaseException = RuntimeError("never")

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        raise self.error


# (raw exception, the kind the host always gave it, the status its route answers). The
# kind comes from the TYPE where there is one (the same cases as before: httpx errors,
# timeouts), from the TEXT where there is not. A builtin ConnectionError is unreachable by
# its type now (a reset used to depend on its text mentioning "errno").
ERRORS = [
    (_status(502), "upstream_error", 502), (_status(503), "upstream_error", 502), (_status(404), "not_found", 404),
    (_status(401), "auth_failed", 502), (_status(403), "auth_failed", 502), (_status(429), "rate_limited", 503),
    (_status(400), "failed", 502),
    (httpx.ConnectError("[Errno 111] Connection refused"), "unreachable", 503),
    (httpx.ConnectTimeout("slow"), "timeout", 503), (httpx.ReadTimeout("slow"), "timeout", 503),
    (httpx.ReadError("reset"), "unreachable", 503), (httpx.RemoteProtocolError("odd"), "failed", 502),
    (TimeoutError("timed out"), "timeout", 503),
    (ConnectionRefusedError("[Errno 111] Connection refused"), "unreachable", 503),
    (ConnectionResetError("reset"), "unreachable", 503),
    (ValueError("unexpected payload"), "failed", 502), (KeyError("k"), "failed", 502),
    (RuntimeError("Lidarr URL not configured"), "failed", 502),
    (RuntimeError("[Errno -3] Temporary failure in name resolution"), "unreachable", 503),
    (RuntimeError("server returned 503"), "upstream_error", 502), (RuntimeError("took too long: timed out"), "timeout", 503),
    (FileNotFoundError("no such file"), "failed", 502), (OSError("disk"), "failed", 502),
]


@pytest.mark.parametrize("error, kind, status", ERRORS, ids=lambda v: f"{type(v).__name__}-{str(v)[:24]}" if isinstance(v, Exception) else str(v))
def test_the_kind_of_a_failure_and_the_status_of_its_route_are_what_they_always_were(error, kind, status):
    holder = []
    proxy = loopback_proxy(lambda c: holder.append(Failing(c)) or holder[0], StaticProviderContext())
    holder[0].error = error

    with pytest.raises(ProviderCallFailed) as failed:
        proxy.get_lyrics(TRACK, SESSION)

    assert host_errors.classify_error(failed.value) == kind
    assert host_errors.http_status_for(host_errors.classify_error(failed.value)) == status
    assert failed.value.exception_type == type(error).__name__
    assert str(failed.value) == (str(error) or type(error).__name__)


def test_the_retry_after_of_a_429_reaches_the_host():
    holder = []
    proxy = loopback_proxy(lambda c: holder.append(Failing(c)) or holder[0], StaticProviderContext())
    holder[0].error = _status(429, {"Retry-After": "120"})

    with pytest.raises(ProviderCallFailed) as failed:
        proxy.get_lyrics(TRACK, SESSION)

    assert (failed.value.kind, failed.value.retry_after) == ("rate_limited", 120)


@pytest.mark.parametrize("error", [
    ProviderRateLimited("quota", retry_after=12), PermissionError("reconnect"), NotImplementedError("no"),
    RemotePlaylistGone("gone"), DownloadDelegated("p", ["d"], handle="h"), DownloadDeclined("p", "code", "why"), HELD,
], ids=lambda e: type(e).__name__)
def test_the_exceptions_of_the_contract_are_raised_as_themselves_on_the_host(error):
    holder = []
    proxy = loopback_proxy(lambda c: holder.append(Failing(c)) or holder[0], StaticProviderContext())
    holder[0].error = error

    with pytest.raises(type(error)) as raised:
        proxy.get_lyrics(TRACK, SESSION)

    assert not isinstance(raised.value, ProviderCallFailed)
    assert raised.value.args == error.args and vars(raised.value) == vars(error)


def test_a_rate_limit_keeps_its_retry_after_for_the_route():
    from routes.errors import provider_retry_after

    holder = []
    proxy = loopback_proxy(lambda c: holder.append(Failing(c)) or holder[0], StaticProviderContext())
    for error, seconds in ((ProviderRateLimited("q", retry_after=346.2), 347), (_status(429, {"retry-after": "9"}), 9),
                           (_status(429), 30), (ProviderRateLimited("q"), 30)):
        holder[0].error = error
        with pytest.raises((ProviderRateLimited, ProviderCallFailed)) as raised:
            proxy.get_lyrics(TRACK, SESSION)
        assert provider_retry_after(raised.value) == seconds


def test_a_failure_by_text_is_still_classified_by_its_text():
    failed = ProviderCallFailed(None, "[Errno -3] Temporary failure in name resolution")

    assert host_errors.classify_error(failed) == "unreachable"
    assert host_errors.classify_error(ProviderCallFailed("not_found", "whatever")) == "not_found"


# ------------------------------------------------------------------
# A transport that fails, a provider that breaks the protocol
# ------------------------------------------------------------------


class Broken:
    """A transport whose replies are given."""

    def __init__(self, replies, *, describe=True) -> None:
        self.replies, self.describe = replies, describe

    def request(self, request):
        if self.describe:
            self.describe = False
            return LoopbackTransport(ProviderRunner(Failing)).request(request)
        if isinstance(self.replies, BaseException):
            raise self.replies
        return iter(self.replies)


def test_a_connection_that_fails_is_an_unreachable_provider():
    proxy = connect(Broken(TransportError("socket closed")), StaticProviderContext())

    with pytest.raises(ProviderCallFailed) as failed:
        proxy.get_lyrics(TRACK, SESSION)

    assert (failed.value.kind, failed.value.exception_type) == ("unreachable", "TransportError")
    assert host_errors.http_status_for(host_errors.classify_error(failed.value)) == 503


def test_a_provider_that_breaks_the_protocol_is_a_failed_call_not_a_crash():
    for replies in ([], [protocol.json_frame({"t": "wat"})], [protocol.bytes_frame(b"x")],
                    [protocol.json_frame({"t": "result", "value": {"$": "Nothing"}, "chunks": False})]):
        proxy = connect(Broken(replies), StaticProviderContext())
        with pytest.raises(ProviderCallFailed) as failed:
            proxy.get_lyrics(TRACK, SESSION)
        assert failed.value.kind == "failed"


def test_a_provider_that_cannot_be_described_cannot_be_connected():
    class Dead:
        def request(self, request):
            raise TransportError("no runner")

    with pytest.raises(ProviderCallFailed) as failed:
        connect(Dead(), StaticProviderContext())
    assert failed.value.kind == "unreachable"

    with pytest.raises(ProviderCallFailed, match="does not declare 'name'"):
        loopback_proxy(type("Nameless", (LyricsProvider,), {
            "label": "x", "auth": NO_AUTH, "validate_config": lambda s, c: True,
            "test_connection": lambda s, x: None, "get_lyrics": lambda s, t, x: None,
        }), StaticProviderContext())


def test_an_argument_the_wire_cannot_carry_is_a_bug_of_the_host_not_a_failed_provider():
    proxy = loopback_proxy(Failing, StaticProviderContext())

    with pytest.raises(wire.WireError):
        proxy.get_lyrics(TRACK, object())


def test_a_provider_declaring_a_type_that_is_not_on_the_wire_cannot_be_described():
    @dataclass(frozen=True)
    class Stranger:
        """Not a type of the SDK: nobody on the other side could rebuild it."""

    class Odd(Failing):
        name = "odd"
        auth = AuthDefinition(
            flow=AuthFlow.OAUTH2, account_model=AccountModel.SYSTEM,
            oauth2=OAuth2Config("a", "t", [], request_auth=Stranger()),
        )

    with pytest.raises(ProviderCallFailed, match="not a wire type"):
        loopback_proxy(Odd, StaticProviderContext())


# ------------------------------------------------------------------
# Streams
# ------------------------------------------------------------------


class Streaming(MetadataProvider, StorageProvider):
    name, label, auth = "streaming", "Streaming", NO_AUTH

    def __init__(self, context) -> None:
        super().__init__(context)
        self.produced, self.closed = 0, False

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def iter_saved_tracks(self, session, page_size=50):
        try:
            for index in range(4):
                self.produced += 1
                if index == 2 and session.extra.get("rate_limit"):
                    raise ProviderRateLimited("quota", retry_after=5)
                if index == 2 and session.extra.get("crash"):
                    raise httpx.ConnectError("reset")
                yield [make_track(f"page {index}")]
        finally:
            self.closed = True

    def store(self, source, relative_dest, session):
        return relative_dest

    def exists(self, relative_path, session):
        return True

    def delete(self, relative_path, session):
        return True

    def stream(self, relative_path, workspace, session, offset=0):
        def chunks():
            try:
                for index in range(3):
                    self.produced += 1
                    if relative_path == "broken" and index == 1:
                        raise ConnectionResetError("lost")
                    yield bytes([index]) * 2
            finally:
                self.closed = True

        return StorageStreamResult(chunks=chunks(), size=6)


@pytest.fixture
def streaming():
    holder = []
    return loopback_proxy(lambda c: holder.append(Streaming(c)) or holder[0], StaticProviderContext()), holder


def test_the_host_asking_for_the_first_page_does_not_make_the_provider_produce_the_rest(streaming):
    proxy, holder = streaming

    pages = proxy.iter_saved_tracks(AuthSession.anonymous())

    assert holder[0].produced == 0
    assert [t.title for t in next(pages)] == ["page 0"]
    assert holder[0].produced == 1
    assert [len(page) for page in pages] == [1, 1, 1]
    assert holder[0].closed


def test_giving_up_a_stream_closes_the_providers_iterator(streaming):
    proxy, holder = streaming

    pages = proxy.iter_saved_tracks(AuthSession.anonymous())
    next(pages)
    pages.close()

    assert holder[0].closed and holder[0].produced == 1


def test_an_error_in_the_middle_of_a_stream_is_raised_at_the_point_of_iteration(streaming):
    proxy, _ = streaming

    pages = proxy.iter_saved_tracks(AuthSession(flow=AuthFlow.NONE, extra={"rate_limit": True}))
    assert len(next(pages)) == 1 and len(next(pages)) == 1
    with pytest.raises(ProviderRateLimited) as limited:
        next(pages)
    assert limited.value.retry_after == 5

    pages = proxy.iter_saved_tracks(AuthSession(flow=AuthFlow.NONE, extra={"crash": True}))
    next(pages), next(pages)
    with pytest.raises(ProviderCallFailed) as failed:
        next(pages)
    assert failed.value.kind == "unreachable"


def test_the_chunks_of_a_file_are_produced_on_demand_and_a_loss_is_raised_while_reading(streaming):
    proxy, holder = streaming

    result = proxy.stream("fine", "/ws", AuthSession.anonymous())
    assert holder[0].produced == 0 and next(result.chunks) == b"\x00\x00"
    assert list(result.chunks) == [b"\x01\x01", b"\x02\x02"] and holder[0].closed

    holder[0].produced = 0
    result = proxy.stream("broken", "/ws", AuthSession.anonymous())
    assert next(result.chunks) == b"\x00\x00"
    with pytest.raises(ProviderCallFailed) as failed:
        next(result.chunks)
    assert failed.value.kind == "unreachable"


def test_closing_the_chunks_closes_the_providers_iterator(streaming):
    proxy, holder = streaming

    result = proxy.stream("fine", "/ws", AuthSession.anonymous())
    next(result.chunks)
    result.chunks.close()

    assert holder[0].closed and holder[0].produced == 1


# ------------------------------------------------------------------
# The context of a call
# ------------------------------------------------------------------


class Reading(LyricsProvider):
    name, label, auth = "reading", "Reading", NO_AUTH

    def validate_config(self, config):
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        c = self.context
        return Lyrics(plain=json.dumps([
            dict(c.settings()), c.http_timeout, c.download_timeout, c.connect_timeout, c.stream_start_timeout,
            c.cover_url("c"), c.stream_url("s"), c.preview_url("p"),
        ]))


def test_the_providers_context_is_the_snapshot_the_host_took_for_that_call(monkeypatch):
    context = HostContextWithoutDatabase("reading", {"market": "IT"})
    proxy = loopback_proxy(Reading, context)
    monkeypatch.setenv("MUSIZEER_PUBLIC_API_PREFIX", "/music/api")

    first = json.loads(proxy.get_lyrics(TRACK, SESSION).plain)
    context._given["market"] = "DE"
    monkeypatch.setenv("MUSIZEER_PUBLIC_API_PREFIX", "/other")
    second = json.loads(proxy.get_lyrics(TRACK, SESSION).plain)

    assert first[0] == {"market": "IT"} and second[0] == {"market": "DE"}
    assert first[5:] == ["/music/api/cover/provider/reading/c", "/music/api/stream/provider/reading/s",
                         "/music/api/preview/reading/p"]
    assert second[5] == "/other/cover/provider/reading/c"
    assert first[1:5] == [10, 60, 10, 5], "the defaults of the catalog"


def test_the_host_context_reads_the_settings_and_the_timeouts_at_every_snapshot(monkeypatch):
    from types import SimpleNamespace

    from services import provider_context

    stored = {"market": "IT"}
    config = SimpleNamespace(http_timeout=1, download_timeout=2, connect_timeout=3, stream_start_timeout=4)
    monkeypatch.setattr(provider_context, "_get_provider_config", lambda name, session=None: dict(stored))
    monkeypatch.setattr(provider_context, "get_config", lambda: config)
    monkeypatch.setenv("MUSIZEER_PUBLIC_API_PREFIX", "/p")
    context = HostProviderContext("reading")

    assert context.snapshot() == ContextSnapshot({"market": "IT"}, 1, 2, 3, 4, "/p")
    stored["market"] = "NL"
    config.http_timeout = 9

    assert context.snapshot() == ContextSnapshot({"market": "NL"}, 9, 2, 3, 4, "/p")


def test_a_provider_cannot_see_the_settings_of_another_call_running_at_the_same_time():
    import threading

    barrier = threading.Barrier(3)

    class Meeting(Reading):
        def get_lyrics(self, track, session):
            barrier.wait(timeout=5)
            return Lyrics(plain=self.context.setting("market"))

    contexts = {m: HostContextWithoutDatabase("reading", {"market": m}) for m in "ABC"}
    runner = ProviderRunner(Meeting)
    proxies = {m: connect(LoopbackTransport(runner), contexts[m]) for m in "ABC"}
    seen = {}

    def work(market):
        seen[market] = proxies[market].get_lyrics(TRACK, SESSION).plain

    import threading as t

    threads = [t.Thread(target=work, args=(m,)) for m in "ABC"]
    [th.start() for th in threads]
    [th.join(10) for th in threads]

    assert seen == {"A": "A", "B": "B", "C": "C"}


# ------------------------------------------------------------------
# The host holds no provider
# ------------------------------------------------------------------


def test_the_registry_holds_only_proxies_and_the_runner_builds_every_provider(monkeypatch):
    built: list[tuple[str, bool]] = []
    original = BaseProvider.__init__

    def recording(self, context):
        stack = traceback.extract_stack()
        built.append((type(self).__name__, any(frame.filename.endswith("musizeer_sdk/runner.py") for frame in stack)))
        original(self, context)

    monkeypatch.setattr(BaseProvider, "__init__", recording)

    registry = ProviderRegistry(discover=False)
    for cls in (Streaming, Reading, Failing):
        registry.register(loopback_proxy(cls, StaticProviderContext(provider_name=cls.name)))

    assert built, "providers were built"
    assert all(by_runner for _name, by_runner in built), [n for n, by_runner in built if not by_runner]
    for category in ("metadata", "download", "lyrics", "storage", "sync", "identification"):
        for provider in registry.names_for_category(category):
            found = getattr(registry, f"get_{category}_provider")(provider)
            assert isinstance(found, ProviderProxy)
            assert not any(isinstance(value, BaseProvider) and not isinstance(value, ProviderProxy)
                           for value in vars(found).values())


def test_a_proxy_is_built_once_per_set_of_roles():
    assert proxy_class(("metadata", "sync")) is proxy_class(("metadata", "sync"))
    assert proxy_class(("metadata",)) is not proxy_class(("sync",))


def test_the_host_cannot_be_handed_a_provider_instance_by_a_test():
    raw = FakeMetadataProvider("raw")

    with pytest.raises(BaseException) as failed:  # pytest.fail is not an Exception
        personal_capabilities(raw)
    assert "as_proxy" in str(failed.value)
    assert personal_capabilities(as_proxy(raw)) == []
    assert metadata_capabilities(as_proxy(raw)) == []


def test_a_fake_registry_gives_the_host_the_same_proxy_for_the_same_provider():
    sync = FakeSyncProvider("s")
    registry = FakeRegistry(syncs=[sync], metadata=[sync])

    assert registry.get_sync_provider("s") is registry.get_metadata_provider("s")
    assert registry.get_sync_provider("s") is as_proxy(sync)


def test_a_provider_the_runner_builds_from_its_class_gets_the_snapshot_not_a_context_of_its_own():
    proxy = host_proxy(Reading, {"market": "FR"})

    assert json.loads(proxy.get_lyrics(TRACK, SESSION).plain)[0] == {"market": "FR"}


def test_the_loopback_benchmark_runs(capsys):
    from tests import provider_loopback_benchmark

    with host_boundary.suspended():  # its baseline hands the host the providers themselves
        assert provider_loopback_benchmark.main(["--repeat", "1", "--search", "2", "--album", "2"]) == 0

    out = capsys.readouterr().out
    for line in ("search_tracks(q, 50)", "cross_enrich_album", "context snapshot of a call"):
        assert line in out
