"""
The socket transport: a provider's runner in a sandbox, reached over the Unix socket the
gateway published for it. The framing is the call protocol's, exactly as the tests' loopback
(``tests/loopback.py``) carries it; only the other end is another process.

**One connection per call.** It is opened when the call starts and closed when the caller is
done with the reply (a stream that is abandoned closes it, and the runner stops producing).
A connection that cannot be opened, breaks off, or ends before the reply is complete is a
:class:`TransportError`, which the proxy turns into ``unreachable``: a sandbox that is down
or restarting is a provider that cannot be reached, never a 500.

**Files cross by workspace** (ADR-0003). A sandbox sees one directory of the host's
workspace tree, ``<workspace_dir>/<provider>``, as ``/workspace``. This transport rewrites
the host paths it sends into the sandbox's:

- the ``workspace`` argument of the calls that take one (found from the contract's own
  signatures) must be a workspace of THIS provider; anything else is refused, because
  the sandbox could not see it;
- a ``WorkspaceFile`` of this provider's tree is rewritten in place. A ``WorkspaceFile`` of
  ANOTHER place of the workspace tree (what a download provider wrote, handed to the storage
  provider; a file held for a later upload) is staged first: linked, or copied when it
  cannot be, into a fresh folder of this provider's tree for the duration of the call,
  so the provider reads that one file and nothing else. A file outside the workspace tree
  is never staged.

Results carry names relative to the workspace, as they do in loopback, so nothing comes back
that needs translating.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from musizeer_sdk import protocol
from musizeer_sdk.interfaces import BaseProvider
from musizeer_sdk.manifest import ROLES
from musizeer_sdk.protocol import Frame, ProtocolError, json_frame, load_message, read_frame

from services.provider_proxy import TransportError
from services.workspace import PROVIDER_NAME, provider_directory

logger = logging.getLogger(__name__)

SANDBOX_WORKSPACE = "/workspace"
CONNECT_TIMEOUT_SECONDS = 5.0
# A call that is silent for this long is given up: a provider that hangs must not hold a
# worker for ever (its own HTTP timeouts are far shorter; the longest silent call is a download).
READ_TIMEOUT_SECONDS = 900.0
_WORKSPACE_FILE_TAG = "WorkspaceFile"


def _workspace_argument_positions() -> dict[str, int]:
    """Method -> index of its ``workspace`` argument among the positional arguments (``self``
    excluded, the session included), read from the contract's signatures."""
    positions: dict[str, int] = {}
    for base in {BaseProvider, *ROLES.values()}:
        for name, member in vars(base).items():
            if name.startswith("_") or not inspect.isfunction(member):
                continue
            parameters = [p for p in inspect.signature(member).parameters if p != "self"]
            if "workspace" in parameters:
                index = parameters.index("workspace")
                if positions.setdefault(name, index) != index:
                    raise AssertionError(f"'{name}' has its workspace argument at two positions")
    return positions


WORKSPACE_ARGUMENTS = _workspace_argument_positions()


class Staging:
    """Where the files of other workspaces were put for one call, removed when it ends."""

    def __init__(self, root: Path, provider: str) -> None:
        self._root = root
        self._provider = provider
        self.directories: list[Path] = []

    def stage(self, source_workspace: str, name: str) -> Path:
        """Make ``name`` of the workspace ``source_workspace`` available in a folder of the
        provider's own tree; returns that folder."""
        base = Path(os.path.realpath(source_workspace))
        if not (base.is_relative_to(os.path.realpath(self._root)) and base != Path(os.path.realpath(self._root))):
            raise TransportError(f"'{source_workspace}' is outside the workspace directory: it is not handed to a provider")
        source = Path(os.path.realpath(base / name))
        if not source.is_relative_to(base) or source == base or not source.is_file():
            raise TransportError(f"'{name}' is not a file of the workspace '{source_workspace}'")
        folder = provider_directory(self._root, self._provider) / uuid.uuid4().hex
        folder.mkdir()
        folder.chmod(0o777)
        self.directories.append(folder)
        target = folder / Path(name)
        current = folder
        for part in Path(name).parts[:-1]:
            current = current / part
            current.mkdir(exist_ok=True)
            current.chmod(0o777)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        return folder

    def cleanup(self) -> None:
        for folder in self.directories:
            shutil.rmtree(folder, ignore_errors=True)
        self.directories.clear()


class WorkspaceRewriter:
    """Rewrites the host paths of one request into the sandbox's."""

    def __init__(self, root: Path, provider: str, staging: Staging) -> None:
        self._root = Path(os.path.normpath(root))
        self._base = self._root / provider
        self._staging = staging

    def sandbox_path(self, host_path: str) -> str | None:
        """The path inside the sandbox of a path in this provider's tree, else None."""
        path = Path(os.path.normpath(host_path))
        if path == self._base:
            return SANDBOX_WORKSPACE
        if self._base in path.parents:
            return f"{SANDBOX_WORKSPACE}/{path.relative_to(self._base).as_posix()}"
        return None

    def _argument(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise TransportError("the workspace argument is not a path")
        mapped = self.sandbox_path(value)
        if mapped is None:
            raise TransportError(
                f"the workspace '{value}' is not in the folder of this provider: its sandbox could not see it",
            )
        return mapped

    def _file(self, node: dict) -> None:
        workspace, name = node.get("workspace"), node.get("name")
        if not isinstance(workspace, str) or not isinstance(name, str):
            raise TransportError("a WorkspaceFile without a workspace and a name")
        mapped = self.sandbox_path(workspace)
        if mapped is None:
            folder = self._staging.stage(workspace, name)
            mapped = self.sandbox_path(str(folder))
            assert mapped is not None
        node["workspace"] = mapped

    def _walk(self, node: Any) -> None:
        if isinstance(node, dict):
            if node.get("$") == _WORKSPACE_FILE_TAG:
                self._file(node)
            else:
                for value in node.values():
                    self._walk(value)
        elif isinstance(node, list):
            for value in node:
                self._walk(value)

    def rewrite(self, message: dict) -> dict:
        if message.get("op") != protocol.OP_CALL:
            return message
        self._walk(message.get("args"))
        self._walk(message.get("kwargs"))
        position = WORKSPACE_ARGUMENTS.get(message.get("method", ""))
        if position is not None:
            kwargs = message.get("kwargs") or {}
            if "workspace" in kwargs:
                kwargs["workspace"] = self._argument(kwargs["workspace"])
            else:
                at = message.get("session_at")
                index = position - 1 if at is not None and at <= position else position
                args = message.get("args")
                if isinstance(args, list) and index < len(args):
                    args[index] = self._argument(args[index])
        return message


class SocketTransport:
    """Carries requests to the runner listening on ``socket_path``.

    ``workspace_root`` is the host's workspace directory (a callable: it is a setting). ``provider``
    names the folder of it that the provider's sandbox sees."""

    def __init__(
        self,
        socket_path: Path,
        provider: str,
        workspace_root: Callable[[], Path],
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        if not PROVIDER_NAME.match(provider):
            raise ValueError(f"'{provider}' is not a provider name")
        self._path = socket_path
        self._provider = provider
        self._workspace_root = workspace_root
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def _connect(self) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._connect_timeout)
            connection.connect(str(self._path))
        except OSError as exc:
            connection.close()
            raise TransportError(f"provider '{self._provider}' cannot be reached: {exc}") from exc
        connection.settimeout(self._read_timeout)
        return connection

    def request(self, request: Frame) -> Iterator[Frame]:
        root = Path(self._workspace_root())
        staging = Staging(root, self._provider)
        connection: socket.socket | None = None
        try:
            try:
                message = load_message(request)
                request = json_frame(WorkspaceRewriter(root, self._provider, staging).rewrite(message))
            except ProtocolError as exc:
                raise TransportError(f"unreadable request: {exc}") from exc
            connection = self._connect()
            try:
                connection.sendall(protocol.pack_frame(request))
            except OSError as exc:
                raise TransportError(f"provider '{self._provider}' broke the connection: {exc}") from exc
            while True:
                try:
                    frame = read_frame(connection.recv)
                except (OSError, ProtocolError) as exc:
                    raise TransportError(f"provider '{self._provider}' broke the connection: {exc}") from exc
                if frame is None:
                    raise TransportError(f"provider '{self._provider}' closed the connection before the reply was complete")
                yield frame
        finally:
            if connection is not None:
                connection.close()
            staging.cleanup()
