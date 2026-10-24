"""The socket transport: a provider's runner in another process, reached over its Unix socket."""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from musizeer_sdk import protocol
from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizeer_sdk.context import StaticProviderContext
from musizeer_sdk.identifiers import Identifier
from musizeer_sdk.interfaces import DownloadProvider, StorageProvider, StorageStreamResult, WorkspaceFile
from musizeer_sdk.runner import ProviderRunner, RunnerServer
from services import provider_proxy
from services.provider_proxy import ProviderCallFailed, TransportError
from services.provider_socket import (
    SANDBOX_WORKSPACE,
    WORKSPACE_ARGUMENTS,
    SocketTransport,
    Staging,
    WorkspaceRewriter,
)

SESSION = AuthSession.anonymous()
NO_AUTH = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)


class Filing(DownloadProvider, StorageProvider):
    """A provider that does file work. Where a sandbox would see ``/workspace``, it finds the
    folder it was given by the test (``home``)."""

    name = "filing"
    label = "Filing"
    auth = NO_AUTH
    supported_identifier_types = {"isrc"}
    home: Path

    def validate_config(self, config): return True
    def test_connection(self, session): return None

    def local(self, path: str) -> Path:
        assert path == SANDBOX_WORKSPACE or path.startswith(SANDBOX_WORKSPACE + "/"), f"{path} is not a sandbox path"
        return self.home / path[len(SANDBOX_WORKSPACE):].lstrip("/")

    def download_by_id(self, identifier, workspace, session):
        self.seen = workspace
        (self.local(workspace) / "song.flac").write_bytes(b"audio")
        return "song.flac"

    def store(self, source, relative_dest, session):
        self.seen = source.workspace
        return self.local(source.workspace).joinpath(source.name).read_text()

    def exists(self, relative_path, session):
        return True

    def delete(self, relative_path, session):
        return True

    def stream(self, relative_path, workspace, session, offset=0):
        self.seen = workspace
        if relative_path == "chunks":
            return StorageStreamResult(chunks=iter([b"ab", b"cd"]), size=4)
        (self.local(workspace) / "out.bin").write_bytes(b"streamed")
        return StorageStreamResult(file="out.bin")


@pytest.fixture
def world(tmp_path):
    """A runner serving ``Filing`` on a socket, and a transport to it. The host's workspace
    directory is ``tmp/ws``; the sandbox's ``/workspace`` is ``tmp/ws/filing``."""
    root = tmp_path / "ws"
    (root / "filing").mkdir(parents=True)
    socket_path = tmp_path / "filing.sock"
    built = []

    def factory(context):
        provider = Filing(context)
        provider.home = root / "filing"
        built.append(provider)
        return provider

    server = RunnerServer(str(socket_path), ProviderRunner(factory))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    transport = SocketTransport(socket_path, "filing", lambda: root)
    proxy = provider_proxy.connect(transport, StaticProviderContext(provider_name="filing"))
    yield type("World", (), {"root": root, "proxy": proxy, "provider": lambda self: built[0], "path": socket_path, "tmp": tmp_path})()
    server.shutdown()
    server.server_close()


def make_workspace(root: Path, provider: str, name: str = "w1") -> Path:
    path = root / provider / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------
# Framing and failures
# ------------------------------------------------------------------


def test_a_provider_behind_a_socket_is_described_and_called_like_any_other(world):
    assert world.proxy.name == "filing" and set(world.proxy.roles) == {"download", "storage"}
    assert world.proxy.exists("a.flac", SESSION) is True
    chunks = world.proxy.stream("chunks", str(make_workspace(world.root, "filing")), SESSION)
    assert b"".join(chunks.chunks) == b"abcd"


def test_a_socket_that_is_not_there_is_unreachable_not_a_crash(tmp_path):
    transport = SocketTransport(tmp_path / "gone.sock", "filing", lambda: tmp_path)

    with pytest.raises(ProviderCallFailed) as raised:
        provider_proxy.connect(transport, StaticProviderContext())

    assert raised.value.kind == "unreachable" and "cannot be reached" in raised.value.message


def test_a_provider_that_goes_away_is_unreachable_afterwards(world):
    world.path.unlink()

    with pytest.raises(ProviderCallFailed) as raised:
        world.proxy.exists("a", SESSION)

    assert raised.value.kind == "unreachable"


def listener(path: Path, behave) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def run():
        connection, _ = server.accept()
        with connection:
            protocol.read_frame(connection.recv)
            behave(connection)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def proxy_over(path: Path, tmp_path: Path):
    from musizeer_sdk.context import StaticProviderContext
    from musizeer_sdk.manifest import build

    manifest = build(Filing(StaticProviderContext()))
    return provider_proxy.proxy_class(manifest.roles)(manifest, SocketTransport(path, "filing", lambda: tmp_path), StaticProviderContext())


def test_a_connection_closed_before_the_reply_is_unreachable(tmp_path):
    path = tmp_path / "p.sock"
    listener(path, lambda connection: None)

    with pytest.raises(ProviderCallFailed) as raised:
        proxy_over(path, tmp_path).exists("a", SESSION)

    assert raised.value.kind == "unreachable" and "closed the connection" in raised.value.message


def test_a_connection_that_breaks_in_the_middle_of_a_frame_is_unreachable(tmp_path):
    path = tmp_path / "p.sock"
    listener(path, lambda connection: connection.sendall(b"\x00\x00\x01\x00\x01{\"t\":"))

    with pytest.raises(ProviderCallFailed) as raised:
        proxy_over(path, tmp_path).exists("a", SESSION)

    assert raised.value.kind == "unreachable"


def test_a_reply_that_is_not_the_protocol_is_a_failure_of_the_provider(tmp_path):
    path = tmp_path / "p.sock"
    listener(path, lambda connection: connection.sendall(protocol.pack_frame(protocol.Frame(protocol.FRAME_JSON, b"[1]"))))

    with pytest.raises(ProviderCallFailed) as raised:
        proxy_over(path, tmp_path).exists("a", SESSION)

    assert raised.value.kind == "failed" and "broke the protocol" in raised.value.message


def test_a_silent_provider_is_given_up_after_the_read_timeout(tmp_path):
    path = tmp_path / "p.sock"
    release = threading.Event()
    listener(path, lambda connection: release.wait(5))
    proxy = provider_proxy.proxy_class(("download", "storage"))
    transport = SocketTransport(path, "filing", lambda: tmp_path, read_timeout=0.2)
    from musizeer_sdk.manifest import build

    instance = proxy(build(Filing(StaticProviderContext())), transport, StaticProviderContext())
    try:
        with pytest.raises(ProviderCallFailed) as raised:
            instance.exists("a", SESSION)
    finally:
        release.set()

    assert raised.value.kind == "unreachable"


def test_abandoning_a_stream_closes_the_connection(world):
    chunks = world.proxy.stream("chunks", str(make_workspace(world.root, "filing")), SESSION).chunks
    assert next(chunks) == b"ab"
    chunks.close()  # the caller is done: nothing is left open (the runner sees the connection end)
    assert world.proxy.exists("again", SESSION) is True


# ------------------------------------------------------------------
# Workspaces
# ------------------------------------------------------------------


def test_the_workspace_argument_of_the_calls_that_take_one_is_found_in_the_contract():
    assert WORKSPACE_ARGUMENTS == {"download_by_id": 1, "download_fuzzy": 1, "stream": 1, "fetch_partial": 1}


def test_a_workspace_of_the_provider_is_sent_as_the_path_the_sandbox_sees(world):
    workspace = make_workspace(world.root, "filing", "w1")

    assert world.proxy.download_by_id(Identifier("isrc", "X"), str(workspace), SESSION) == "song.flac"

    assert world.provider().seen == "/workspace/w1"
    assert (workspace / "song.flac").read_bytes() == b"audio", "the provider wrote it where the host looks"


def test_a_workspace_by_keyword_and_a_nested_one_are_rewritten_too(world):
    workspace = make_workspace(world.root, "filing", "w2")

    result = world.proxy.stream("file", workspace=str(workspace), session=SESSION)

    assert result.file == "out.bin" and world.provider().seen == "/workspace/w2"
    nested = make_workspace(world.root, "filing", "w3/inner")
    world.proxy.stream("file", str(nested), SESSION)
    assert world.provider().seen == "/workspace/w3/inner"


@pytest.mark.parametrize("where", ["other/w1", "filing-evil/w1", "../outside", "."])
def test_a_workspace_that_is_not_in_the_folder_of_the_provider_is_refused(world, where):
    path = world.root / where
    path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ProviderCallFailed, match="not in the folder of this provider"):
        world.proxy.download_by_id(Identifier("isrc", "X"), str(path), SESSION)


def test_a_file_of_the_providers_own_workspace_is_sent_in_place(world):
    workspace = make_workspace(world.root, "filing", "w1")
    (workspace / "a.txt").write_text("hello")

    assert world.proxy.store(WorkspaceFile(str(workspace), "a.txt"), "dest", SESSION) == "hello"
    assert world.provider().seen == "/workspace/w1"


def test_a_file_of_another_providers_workspace_is_staged_for_the_call_and_removed_after(world):
    other = make_workspace(world.root, "downloader", "w9")
    (other / "sub").mkdir()
    (other / "sub" / "song.flac").write_text("the audio")

    result = world.proxy.store(WorkspaceFile(str(other), "sub/song.flac"), "dest", SESSION)

    assert result == "the audio"
    staged = world.provider().seen
    assert staged.startswith("/workspace/") and staged != "/workspace/w9", "the provider got a folder of its own tree"
    assert list((world.root / "filing").iterdir()) == [], "the staged copy is gone when the call ends"
    assert (other / "sub" / "song.flac").read_text() == "the audio", "the original is untouched"
    assert os.stat(other / "sub" / "song.flac").st_nlink == 1


def test_a_file_held_for_a_later_upload_is_staged_the_same_way(world):
    held = world.root / "held-uploads" / "track"
    held.mkdir(parents=True)
    (held / "song.flac").write_text("held")

    assert world.proxy.store(WorkspaceFile(str(held), "song.flac"), "dest", SESSION) == "held"


def test_staging_only_ever_hands_over_a_file_of_the_workspace_tree(world, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    inside = make_workspace(world.root, "downloader", "w9")
    (inside / "link").symlink_to(secret)
    (world.root / "other-file").write_text("x")

    for source in (WorkspaceFile(str(tmp_path), "secret.txt"), WorkspaceFile(str(inside), "../../../secret.txt"),
                   WorkspaceFile(str(inside), "link"), WorkspaceFile(str(inside), "missing"), WorkspaceFile(str(world.root), "other-file"),
                   WorkspaceFile("/etc", "passwd")):
        with pytest.raises(ProviderCallFailed):
            world.proxy.store(source, "dest", SESSION)
    assert list((world.root / "filing").iterdir()) == []


def test_staging_links_the_file_and_falls_back_to_a_copy(world, monkeypatch):
    other = make_workspace(world.root, "downloader", "w9")
    (other / "a.bin").write_bytes(b"1234")
    staging = Staging(world.root, "filing")

    folder = staging.stage(str(other), "a.bin")
    assert os.stat(folder / "a.bin").st_nlink == 2

    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-device")))
    copied = staging.stage(str(other), "a.bin")
    assert (copied / "a.bin").read_bytes() == b"1234" and os.stat(copied / "a.bin").st_ino != os.stat(other / "a.bin").st_ino
    staging.cleanup()
    assert not folder.exists() and not copied.exists()


def test_the_rewriter_leaves_everything_that_is_not_a_workspace_alone(tmp_path):
    rewriter = WorkspaceRewriter(tmp_path, "filing", Staging(tmp_path, "filing"))
    message = json.loads(protocol.call_request("search_tracks", ("/downloads/x", 5), {"note": "/downloads/y"}, None).payload)

    assert rewriter.rewrite(json.loads(json.dumps(message))) == message
    describe = json.loads(protocol.describe_request().payload)
    assert rewriter.rewrite(describe) == describe


def test_the_session_is_taken_out_of_the_arguments_before_the_workspace_is_found(tmp_path):
    workspace = make_workspace(tmp_path, "filing", "w1")
    rewriter = WorkspaceRewriter(tmp_path, "filing", Staging(tmp_path, "filing"))
    frame = protocol.call_request("download_by_id", (Identifier("isrc", "X"), str(workspace), SESSION), {}, None)
    message = json.loads(frame.payload)

    assert message["session_at"] == 2 and len(message["args"]) == 2
    assert rewriter.rewrite(message)["args"][1] == "/workspace/w1"
