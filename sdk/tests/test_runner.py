"""The runner dispatches decoded requests to a provider it built itself."""
from __future__ import annotations

import os
import socket
import tempfile
import threading

import pytest

from musizeer_sdk import protocol, wire
from musizeer_sdk.auth import AuthSession
from musizeer_sdk.context import ContextSnapshot
from musizeer_sdk.errors import (
    DelegateHoldsRelease,
    DownloadDeclined,
    DownloadDelegated,
    ProviderRateLimited,
    RemotePlaylistGone,
)
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.interfaces import StorageStreamResult
from musizeer_sdk.manifest import Manifest
from musizeer_sdk.models import Lyrics
from musizeer_sdk.protocol import Frame, FrameSocket
from musizeer_sdk.runner import ProviderRunner, RunnerServer
from sample_providers import (
    NO_AUTH,
    Base,
    SampleDownload,
    SampleLyrics,
    SampleMetadata,
    SampleStorage,
    SampleSync,
    session,
    track,
)
from musizeer_sdk.interfaces import LyricsProvider


class Remote(Exception):
    def __init__(self, error: protocol.RemoteError) -> None:
        super().__init__(error.message)
        self.error = error


def ask(runner: ProviderRunner, method: str, *args, context: ContextSnapshot | None = None, **kwargs):
    return protocol.read_reply(runner.handle(protocol.call_request(method, args, kwargs, context)), Remote)


def describe(runner: ProviderRunner, context: ContextSnapshot | None = None) -> Manifest:
    return protocol.read_reply(runner.handle(protocol.describe_request(context)), Remote)


# --- building and describing ------------------------------------------------


def test_the_runner_builds_the_provider_itself_and_only_when_first_asked():
    built = []

    def factory(context):
        built.append(context)
        return SampleMetadata(context)

    runner = ProviderRunner(factory)
    assert built == []

    manifest = describe(runner)
    describe(runner)
    ask(runner, "search_tracks", "q", session(), 1)

    assert len(built) == 1
    assert manifest.name == "samplemeta" and manifest.roles == ("metadata",)


def test_describe_gives_the_manifest_of_the_provider():
    manifest = describe(ProviderRunner(SampleStorage))

    assert manifest.roles == ("storage",) and manifest.declarations["supports_stream_offset"] is True


def test_a_provider_that_cannot_be_built_or_described_is_an_error_not_a_crash():
    def broken(context):
        raise RuntimeError("no module named x")

    with pytest.raises(Remote, match="no module named x"):
        describe(ProviderRunner(broken))

    class Nameless(Base, LyricsProvider):
        def get_lyrics(self, track, session):
            return None

    with pytest.raises(Remote, match="does not declare 'name'"):
        describe(ProviderRunner(Nameless))


# --- calls ------------------------------------------------------------------


def test_a_call_runs_the_method_with_the_arguments_and_returns_the_decoded_result():
    runner = ProviderRunner(SampleMetadata)

    found = ask(runner, "search_tracks", "love", session(), 3)

    assert [t.title for t in found] == ["love 0", "love 1", "love 2"]
    assert ask(runner, "find_by_isrc", "ISRC1", session()).external_ids == [Identifier(type="isrc", value="ISRC1")]
    assert ask(runner, "get_track", "missing", session()) is None


def test_arguments_given_by_keyword_arrive_by_keyword():
    runner = ProviderRunner(SampleMetadata)

    assert len(ask(runner, "search_tracks", "q", session=session(), limit=2)) == 2


def test_the_session_arrives_where_the_caller_put_it():
    seen = []

    class Echo(Base, LyricsProvider):
        name, label, auth = "echo", "Echo", NO_AUTH

        def get_lyrics(self, track, session):
            seen.append(session)
            return Lyrics(plain=track.title)

    runner = ProviderRunner(Echo)
    mine = AuthSession.oauth2(headers={"Authorization": "Bearer t"})

    assert ask(runner, "get_lyrics", track("Hi"), mine) == Lyrics(plain="Hi")
    assert ask(runner, "get_lyrics", track("Hi"), session=mine) == Lyrics(plain="Hi")
    assert seen == [mine, mine]


def test_only_the_methods_of_the_contract_can_be_called():
    runner = ProviderRunner(SampleMetadata)

    for method in ("read_settings", "_private", "__init__", "context", "nope", "search_albums_please"):
        with pytest.raises(Remote, match="not a method of the contract"):
            ask(runner, method, session())
    # A method of another role is not callable either.
    with pytest.raises(Remote, match="not a method of the contract"):
        ask(runner, "get_lyrics", track(), session())


def test_a_wrong_call_is_an_error_reply():
    runner = ProviderRunner(SampleMetadata)

    with pytest.raises(Remote) as caught:
        ask(runner, "search_tracks")

    assert caught.value.error.type_name == "TypeError"


def test_a_request_that_is_not_one_is_an_error_reply():
    runner = ProviderRunner(SampleMetadata)

    for frame in (Frame(protocol.FRAME_BYTES, b"x"), protocol.json_frame({"v": 1, "op": "call", "method": "x"}),
                  protocol.json_frame({"v": 7, "op": "call"})):
        with pytest.raises(Remote):
            protocol.read_reply(runner.handle(frame), Remote)


# --- the context of a call --------------------------------------------------


def test_the_providers_context_is_built_from_the_snapshot_of_the_call():
    runner = ProviderRunner(SampleMetadata)
    snapshot = ContextSnapshot(
        settings={"market": "FR"}, http_timeout=11, download_timeout=22, connect_timeout=33,
        stream_start_timeout=44, api_prefix="/music/api",
    )

    found = ask(runner, "search_tracks", "q", session(), 1, context=snapshot)[0]

    assert found.cover_url == "/music/api/cover/provider/samplemeta/c0"
    assert found.stream_url == "/music/api/stream/provider/samplemeta/s0"
    assert found.preview_url == "/music/api/preview/samplemeta/p0"
    provider = runner._provider
    ask(runner, "get_new_releases", session(), context=snapshot)
    assert provider.seen_settings == [{"market": "FR", "http": 11, "download": 22, "connect": 33, "stream": 44}]


def test_a_setting_changed_between_two_calls_is_seen_by_the_next_one():
    runner = ProviderRunner(SampleMetadata)
    provider = ProviderRunner._provider_instance(runner)

    ask(runner, "get_new_releases", session(), context=ContextSnapshot(settings={"market": "IT"}))
    ask(runner, "get_new_releases", session(), context=ContextSnapshot(settings={"market": "DE"}))
    ask(runner, "get_new_releases", session())

    assert [seen["market"] for seen in provider.seen_settings] == ["IT", "DE", None]
    assert provider.seen_settings[2]["http"] == ContextSnapshot().http_timeout


def test_concurrent_calls_each_see_their_own_snapshot():
    runner = ProviderRunner(SampleMetadata)
    provider = runner._provider_instance()
    barrier = threading.Barrier(4)
    outcome = {}

    def get_new_releases(session, limit=20):
        barrier.wait(timeout=5)  # every call is inside the provider at the same time
        return [track(provider.context.setting("market"))]

    provider.get_new_releases = get_new_releases

    def worker(market):
        outcome[market] = ask(runner, "get_new_releases", session(), context=ContextSnapshot(settings={"market": market}))

    threads = [threading.Thread(target=worker, args=(m,)) for m in "ABCD"]
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert {market: [t.title for t in found] for market, found in outcome.items()} == {m: [m] for m in "ABCD"}


def test_a_pure_query_is_called_without_the_snapshot():
    runner = ProviderRunner(SampleDownload)
    snapshot = ContextSnapshot(settings={"never": "reads"})

    # The sample's supports_identifier would say False if it saw the setting.
    assert ask(runner, "supports_identifier", Identifier(type="isrc", value="x"), context=snapshot) is True


# --- errors -----------------------------------------------------------------


def test_an_exception_of_the_provider_reaches_the_host_as_a_kind_and_a_message():
    runner = ProviderRunner(SampleMetadata)

    with pytest.raises(Remote) as caught:
        ask(runner, "get_track", "x1", session())

    assert caught.value.error == protocol.RemoteError(None, "cannot read x1", None, "RuntimeError")


def test_the_runner_classifies_by_type_where_the_types_exist():
    class Failing(Base, LyricsProvider):
        name, label, auth = "failing", "Failing", NO_AUTH
        errors = {
            "timeout": TimeoutError("timed out"),
            "reset": ConnectionResetError("reset by peer"),
        }

        def get_lyrics(self, track, session):
            raise self.errors[track.title]

    runner = ProviderRunner(Failing)
    for title, kind in (("timeout", "timeout"), ("reset", "unreachable")):
        with pytest.raises(Remote) as caught:
            ask(runner, "get_lyrics", track(title), session())
        assert caught.value.error.kind == kind


def test_the_exceptions_of_the_contract_are_raised_again_as_themselves():
    meta, download, sync = ProviderRunner(SampleMetadata), ProviderRunner(SampleDownload), ProviderRunner(SampleSync)

    with pytest.raises(PermissionError, match="missing scope"):
        ask(meta, "unlike_saved_track", "id", session())
    with pytest.raises(ProviderRateLimited) as limited:
        ask(meta, "get_artist", "id", session())
    assert (str(limited.value), limited.value.retry_after) == ("slow down", 7)
    with pytest.raises(NotImplementedError):
        ask(download, "dispatch_album", None, session())
    with pytest.raises(DownloadDelegated) as delegated:
        ask(download, "download_by_id", Identifier(type="isrc", value="delegate"), "ws", session())
    assert (delegated.value.provider, delegated.value.details, delegated.value.handle) == ("sampledl", ["queued"], "h1")
    with pytest.raises(DownloadDeclined) as declined:
        ask(download, "download_by_id", Identifier(type="isrc", value="decline"), "ws", session())
    assert (declined.value.provider, declined.value.code, declined.value.message) == ("sampledl", "have_release", "already there")
    with pytest.raises(DelegateHoldsRelease) as holds:
        ask(download, "download_fuzzy", track("T"), "ws", session())
    assert (holds.value.handle, holds.value.complete, holds.value.release, holds.value.state) == ("h2", False, "Album", {"a": 1})
    assert [t.title for t in holds.value.tracks] == ["T"]
    with pytest.raises(RemotePlaylistGone):
        ask(sync, "update_playlist", "r", "n", [], session())


# --- streams ----------------------------------------------------------------


def make_runner(cls):
    holder = []

    def factory(context):
        holder.append(cls(context))
        return holder[0]

    return ProviderRunner(factory), holder


def test_the_pages_of_a_stream_are_produced_as_the_host_asks_for_them():
    runner, built = make_runner(SampleMetadata)

    pages = ask(runner, "iter_saved_tracks", session(), 10, context=ContextSnapshot(settings={"market": "IT"}))
    assert built[0].pages_produced == 0

    first = next(pages)
    assert [t.title for t in first] == ["page 0 IT"]
    assert built[0].pages_produced == 1, "asking for the first page does not make the provider produce the rest"
    assert [t.title for page in pages for t in page] == ["page 1 IT", "page 2 IT", "page 3 IT", "page 4 IT"]


def test_a_stream_sees_the_snapshot_of_its_call_at_every_step():
    runner, _ = make_runner(SampleMetadata)

    pages = ask(runner, "iter_saved_tracks", session(), 10, context=ContextSnapshot(settings={"market": "IT"}))
    other = ask(runner, "iter_saved_tracks", session(), 10, context=ContextSnapshot(settings={"market": "NL"}))

    assert [next(pages)[0].title, next(other)[0].title, next(pages)[0].title, next(other)[0].title] == [
        "page 0 IT", "page 0 NL", "page 1 IT", "page 1 NL",
    ]


def test_an_error_in_the_middle_of_a_stream_is_raised_at_that_point_of_the_iteration():
    runner, built = make_runner(SampleMetadata)

    pages = ask(runner, "iter_saved_tracks", session(explode=True), 10)

    assert [len(next(pages)) for _ in range(3)] == [1, 1, 1]
    with pytest.raises(ProviderRateLimited) as limited:
        next(pages)
    assert limited.value.retry_after == 12
    assert built[0].closed


def test_giving_up_on_a_stream_closes_the_providers_iterator():
    runner, built = make_runner(SampleMetadata)

    pages = ask(runner, "iter_saved_tracks", session(), 10)
    next(pages)
    assert not built[0].closed
    pages.close()

    assert built[0].closed and built[0].pages_produced == 1


def test_list_files_streams_names_and_none_stays_none():
    runner = ProviderRunner(SampleStorage)

    assert list(ask(runner, "list_files", "lib/", session())) == ["lib/a.mp3", "lib/b.mp3"]
    assert ask(runner, "list_files", "none", session()) is None


def test_a_file_streamed_by_a_storage_arrives_as_chunks_produced_on_demand():
    runner, built = make_runner(SampleStorage)

    result = ask(runner, "stream", "a.mp3", "/ws", session(), offset=5)

    assert isinstance(result, StorageStreamResult)
    assert (result.size, result.offset, result.media_type, result.file) == (12, 5, "audio/mpeg", None)
    assert built[0].chunks_produced == 0
    assert next(result.chunks) == b"\x00\x00\x00"
    assert built[0].chunks_produced == 1
    assert list(result.chunks) == [b"\x01\x01\x01", b"\x02\x02\x02", b"\x03\x03\x03"]
    assert built[0].chunks_closed


def test_giving_up_on_chunks_closes_the_providers_iterator():
    runner, built = make_runner(SampleStorage)

    result = ask(runner, "stream", "a.mp3", "/ws", session())
    next(result.chunks)
    result.chunks.close()

    assert built[0].chunks_closed and built[0].chunks_produced == 1


def test_a_connection_lost_in_the_middle_of_a_file_is_raised_while_reading_it():
    runner, _ = make_runner(SampleStorage)

    result = ask(runner, "stream", "broken", "/ws", session())

    assert [next(result.chunks) for _ in range(2)] == [b"\x00\x00\x00", b"\x01\x01\x01"]
    with pytest.raises(Remote) as caught:
        next(result.chunks)
    assert (caught.value.error.kind, caught.value.error.type_name) == ("unreachable", "ConnectionResetError")


def test_the_pages_and_chunks_of_the_wire_are_frames_of_the_right_type():
    runner = ProviderRunner(SampleStorage)

    frames = list(runner.handle(protocol.call_request("stream", ("a.mp3", "/ws", session()), {}, None)))

    assert [f.kind for f in frames] == [protocol.FRAME_JSON] + [protocol.FRAME_BYTES] * 4 + [protocol.FRAME_JSON]
    assert protocol.load_message(frames[0])["chunks"] is True
    assert protocol.load_message(frames[-1]) == {"t": "end"}


# --- a real socket ----------------------------------------------------------


@pytest.fixture
def served():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "runner.sock")
    runner, built = make_runner(SampleMetadata)
    server = RunnerServer(path, runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path, built
    server.shutdown()
    server.server_close()
    thread.join(5)


def connect(path: str) -> FrameSocket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(path)
    return FrameSocket(sock)


def over(client: FrameSocket, request: Frame):
    """The reply to ``request``, read from the connection as far as the protocol
    says it goes: one frame, or up to the end (or the error) of a stream."""
    client.send(request)

    def frames():
        first = client.receive()
        yield first
        message = protocol.load_message(first)
        if message["t"] in {"manifest", "error"} or (message["t"] == "result" and not message.get("chunks")):
            return
        while True:
            frame = client.receive()
            yield frame
            if frame.kind == protocol.FRAME_JSON and protocol.load_message(frame)["t"] in {"end", "error"}:
                return

    return protocol.read_reply(frames(), Remote)


def test_the_runner_serves_describe_calls_and_streams_over_a_unix_socket(served):
    path, built = served
    client = connect(path)

    assert over(client, protocol.describe_request()).name == "samplemeta"
    assert [t.title for t in over(client, protocol.call_request("search_tracks", ("q", session(), 2), {}, None))] == ["q 0", "q 1"]
    pages = over(client, protocol.call_request("iter_saved_tracks", (session(), 10), {}, ContextSnapshot(settings={"market": "X"})))
    assert [t.title for page in pages for t in page] == [f"page {i} X" for i in range(5)]
    with pytest.raises(PermissionError):
        over(client, protocol.call_request("unlike_saved_track", ("id", session()), {}, None))
    assert built[0].pages_produced == 5


def test_the_socket_is_the_owners_alone(served):
    path, _ = served

    assert os.stat(path).st_mode & 0o777 == 0o600


def test_many_connections_are_served_at_once(served):
    path, _ = served
    results = {}

    def work(index):
        client = connect(path)
        results[index] = [
            len(over(client, protocol.call_request("search_tracks", ("q", session(), index + 1), {}, None)))
            for _ in range(3)
        ]

    threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert results == {i: [i + 1] * 3 for i in range(5)}


def test_a_connection_that_speaks_garbage_is_dropped_and_the_runner_goes_on(served):
    path, _ = served
    bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bad.settimeout(5)
    bad.connect(path)
    bad.sendall(b"\xff\xff\xff\xff\x09garbage")
    try:
        dropped = bad.recv(10) == b""
    except ConnectionResetError:  # the server closed with unread bytes: also a drop
        dropped = True
    assert dropped
    bad.close()

    assert over(connect(path), protocol.describe_request()).name == "samplemeta"


def test_a_stale_socket_file_is_replaced():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "runner.sock")
    open(path, "w").close()

    server = RunnerServer(path, ProviderRunner(SampleLyrics))
    try:
        assert os.path.exists(path)
    finally:
        server.server_close()
    assert not os.path.exists(path)
