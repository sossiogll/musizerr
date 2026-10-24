"""
The host's side of the provider gateway's control socket (``<run dir>/gateway.sock``).

The gateway owns the provider processes; the host asks it which are running (and where
their sockets are), and tells it what the settings allow the sandboxes to reach. The host
never installs or starts anything itself.

**Grants.** A provider may reach the destinations its manifest declares, the ones its
``egress_from_settings`` settings name (a server URL, a host), any port of the host a ``tcp_from_settings``
setting names (FTP) and use the folders its ``filesystem_from_settings`` settings hold. Only the host knows
those values, so before a call it sends the gateway what they are NOW
(:class:`GatewayProviderContext`, which wraps the provider's context: every call's snapshot
is taken through it). The destinations expire after ``GRANT_TTL`` seconds, so a host that stops asking
withdraws them, and the grant is repeated before it runs out (a folder stays until another grant replaces
it: a mount cannot lapse without restarting the provider). It is sent again at once when the
values change. The gateway is not on the data path of calls (they go straight to the
provider's socket) and the sandbox cannot influence what is granted: the values come from the
host's settings and reach the gateway over a socket the sandbox cannot see.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from musizeer_sdk.context import ContextSnapshot
from musizeer_sdk.permissions import Permissions, from_json, path_values, settings_targets, tcp_hosts

from services.provider_context import HostProviderContext

logger = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/musizeer/gateway.sock"
SOCKET_ENVIRONMENT = "PROVIDER_GATEWAY_SOCKET"
REQUEST_TIMEOUT_SECONDS = 5.0
GRANT_TTL = 120.0
GRANT_REFRESH_AFTER = 60.0
# A grant that changes the folders of a provider restarts it, and the gateway answers once the new process runs.
GRANT_TIMEOUT_SECONDS = 20.0
MAX_REPLY_BYTES = 8 * 1024 * 1024


class GatewayUnreachable(Exception):
    """The gateway does not answer (not deployed, down, or its reply is not one)."""


@dataclass(frozen=True)
class GatewayProvider:
    name: str
    state: str
    reason: str
    socket: str | None
    declared: dict | None
    restarts: int = 0


@dataclass(frozen=True)
class GatewayListing:
    epoch: str
    generation: int
    workspace_root: str
    sandbox_available: bool | None
    sandbox_reason: str
    providers: tuple[GatewayProvider, ...] = ()
    rejected: dict[str, str] = field(default_factory=dict)
    # The identifier of the desired set the host last pushed (``services/gateway_sync.py``); None: none yet.
    desired: str | None = None


class GatewayClient:
    def __init__(self, socket_path: str | os.PathLike | None = None, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.socket_path = Path(socket_path or os.environ.get(SOCKET_ENVIRONMENT, DEFAULT_SOCKET))
        self._timeout = timeout

    @property
    def run_dir(self) -> Path:
        """The directory the gateway shares with the host: provider sockets are relative to it."""
        return self.socket_path.parent

    def deployed(self) -> bool:
        return self.socket_path.exists()

    def request(self, payload: dict, timeout: float | None = None) -> dict:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._timeout if timeout is None else timeout)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps(payload).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                reply += chunk
                if len(reply) > MAX_REPLY_BYTES:
                    raise GatewayUnreachable("the gateway's reply is too large")
            document = json.loads(reply)
        except (OSError, ValueError) as exc:
            raise GatewayUnreachable(f"the provider gateway at {self.socket_path} does not answer: {exc}") from exc
        finally:
            connection.close()
        if not isinstance(document, dict) or not document.get("ok"):
            raise GatewayUnreachable(f"the provider gateway refused: {document.get('error') if isinstance(document, dict) else document!r}")
        return document

    def listing(self) -> GatewayListing:
        document = self.request({"op": "list"})
        try:
            sandbox = document.get("sandbox") or {}
            return GatewayListing(
                epoch=document["epoch"],
                generation=int(document["generation"]),
                workspace_root=document["workspace_root"],
                sandbox_available=sandbox.get("available"),
                sandbox_reason=str(sandbox.get("reason", "")),
                providers=tuple(
                    GatewayProvider(
                        name=item["name"], state=item["state"], reason=item.get("reason", ""), socket=item.get("socket"),
                        declared=item.get("declared"), restarts=int(item.get("restarts", 0)),
                    )
                    for item in document["providers"]
                ),
                rejected=dict(document.get("rejected") or {}),
                desired=document.get("desired"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayUnreachable(f"the provider gateway's listing is malformed: {exc!r}") from exc

    def health(self) -> dict:
        return self.request({"op": "health"})

    def install(self, providers: list[dict], generation: str) -> dict:
        """Push the desired set (``services/gateway_sync.py``). The entries may carry a credential: it
        goes to the gateway over its socket and nowhere else, and no message here repeats the request."""
        return self.request({"op": "install", "providers": providers, "generation": generation})

    def grant_egress(
        self, provider: str, targets: tuple[str, ...], ttl: float,
        tcp: dict[str, tuple[str, ...]] | None = None, paths: dict[str, str] | None = None,
    ) -> None:
        """Tell the gateway what the settings of ``provider`` name now. ``tcp`` and ``paths`` are sent only
        when there is something in them (the gateway reads their absence as empty). A change in the folders
        restarts the provider and the reply waits for the new process, so this waits longer than the others."""
        payload: dict = {"op": "egress", "provider": provider, "targets": list(targets), "ttl": ttl}
        if tcp:
            payload["tcp"] = {setting: list(hosts) for setting, hosts in tcp.items()}
        if paths:
            payload["paths"] = dict(paths)
        self.request(payload, timeout=GRANT_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class Grant:
    """What the settings of one provider name at some moment: destinations, raw-TCP hosts, folders."""

    targets: tuple[str, ...] = ()
    tcp: tuple[tuple[str, tuple[str, ...]], ...] = ()
    paths: tuple[tuple[str, str], ...] = ()


class EgressGrants:
    """What this process has told the gateway about the destinations, hosts and folders the settings of each
    provider name, so that it tells it again only when they change or before the grant expires."""

    def __init__(self, client: GatewayClient, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._sent: dict[str, tuple[Grant, float]] = {}
        self._lock = threading.Lock()

    def ensure(self, provider: str, targets: tuple[str, ...], tcp: dict[str, tuple[str, ...]] | None = None, paths: dict[str, str] | None = None) -> None:
        """Make sure the gateway allows exactly these (besides the manifest's) for a while.
        Never raises: if the gateway cannot be told, the call is made anyway and the sandbox's
        own egress decides (it refuses what it was not told)."""
        grant = Grant(targets, tuple(sorted((setting, tuple(hosts)) for setting, hosts in (tcp or {}).items())), tuple(sorted((paths or {}).items())))
        with self._lock:
            sent = self._sent.get(provider)
            now = self._clock()
            if sent is not None and sent[0] == grant and now - sent[1] < GRANT_REFRESH_AFTER:
                return
            try:
                self._client.grant_egress(provider, targets, GRANT_TTL, tcp, paths)
            except GatewayUnreachable as exc:
                logger.warning("could not tell the gateway what %s may reach: %s", provider, exc)
                self._sent.pop(provider, None)
                return
            self._sent[provider] = (grant, now)


class GatewayProviderContext(HostProviderContext):
    """The host's context of a provider that runs behind the gateway: besides what every
    context does, each call's snapshot first tells the gateway what the provider's settings
    allow it to reach (hosts, raw-TCP hosts, folders)."""

    def __init__(self, provider_name: str, permissions: Permissions, grants: EgressGrants) -> None:
        super().__init__(provider_name)
        self._permissions = permissions
        self._grants = grants

    def snapshot(self) -> ContextSnapshot:
        snapshot = super().snapshot()
        permissions = self._permissions
        if permissions.egress_from_settings or permissions.tcp_from_settings or permissions.filesystem_from_settings:
            self._grants.ensure(
                self._provider_name, settings_targets(permissions, snapshot.settings),
                tcp_hosts(permissions, snapshot.settings), path_values(permissions, snapshot.settings),
            )
        return snapshot


def parse_permissions(declared: dict[str, Any]) -> Permissions:
    """The permissions the gateway reports for a provider (they come from its verified image)."""
    return from_json(json.dumps(declared))
