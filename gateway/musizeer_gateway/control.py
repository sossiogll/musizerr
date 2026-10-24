"""
The control socket (``<run_dir>/gateway.sock``): how the host learns what the gateway
runs and tells it what the settings allow.

One JSON object per line in, one JSON line out, over a Unix socket only its owner (the
host's containers share the volume) can open. The operations:

``{"op": "list"}``
    The state of the gateway: its ``epoch`` (changes when it restarts), the ``desired`` generation
    the host last pushed (null: none yet), its
    ``generation`` (changes when the set of running providers does), the workspace
    directory it hands the sandboxes subtrees of, whether a sandbox can be made, and each
    installed provider with its state, the manifest its image declares, the path of its
    socket (relative to the run directory) and its egress (fixed, granted, refused).
    Also the gateway container's pids against its limit (`container`: `pids`, `pids_limit`, null when none is set), and per
    provider the tasks (processes and threads) of its sandbox against `limits.processes` (`tasks`: `current`,
    `peak`, `limit`) and how often it reported that the kernel would not create a thread (`task_limit_hits`).
``{"op": "health"}``
    Just the verdict: ``ok``, ``degraded`` (a provider is down) or ``unavailable`` (no
    sandbox can be made: nothing runs), with the reason.
``{"op": "egress", "provider": name, "targets": ["host:port", ...], "ttl": seconds,
"tcp": {setting: [host, ...]}, "paths": {setting: "/path"}}``
    What the provider's settings name right now (the host alone knows them): the destinations of
    ``egress_from_settings`` (``targets``), the hosts of ``tcp_from_settings`` (``tcp``, per setting; the
    ports are the manifest's) and the folders of ``filesystem_from_settings`` (``paths``, per setting;
    the mode is the manifest's, the gateway judges each path, ``filesystem.py``). They replace the
    previous grant. Destinations expire after ``ttl``; folders stay until replaced, and a change in the
    ones bound restarts the provider (the reply waits for the new process).
``{"op": "install", "providers": [...], "generation": "..."}``
    The desired set, pushed by the host (the database of the application is the only source of
    truth for what is installed; the gateway keeps no list of its own, so after a restart it runs
    nothing until the host pushes the set again). It REPLACES the previous set: the new are
    started, the removed stopped, the ones whose entry changed restarted. Entries are described in
    ``desired.py``; one that cannot be used is refused with a reason, the others are applied.
    A credential in an entry is for pulling that image: it is kept in memory until the image is
    unpacked and never written, logged or reported. ``generation`` is an identifier the host gives
    the set; ``list`` reports it back (``desired``) so the host can tell that the gateway has the set
    it wants.
"""
from __future__ import annotations

import json
import logging
import os
import socketserver
from pathlib import Path

from musizeer_sdk.permissions import to_json

from .config import GatewayConfig
from .desired import DesiredError, parse_set
from .supervisor import RUNNING, ManagedProvider, Supervisor
from .tasks import read_pids_cgroup

# The version of the gateway (ADR-0007 §2), written by the build from the release tag; "dev" otherwise.
VERSION = os.environ.get("MUSIZERR_VERSION") or "dev"

logger = logging.getLogger(__name__)

MAX_LINE = 1024 * 1024
MAX_TTL = 3600.0


def provider_view(managed: ManagedProvider, run_dir: Path) -> dict:
    status = managed.status
    declared = status.declared
    policy = status.policy
    socket_path = managed.socket_path
    return {
        "name": status.entry.name,
        "image": status.entry.reference,
        "state": status.state,
        "reason": status.reason,
        "restarts": status.restarts,
        "started_at": status.started_at,
        "socket": str(socket_path.relative_to(run_dir)) if status.state == RUNNING else None,
        "declared": None if declared is None else {
            "name": declared.name,
            "roles": list(declared.roles),
            "provider_api_version": declared.provider_api_version,
            "permissions": json.loads(to_json(declared.permissions)),
        },
        "egress": None if policy is None else {
            "fixed": sorted(f"{h}:{p}" for h, p in policy.fixed),
            "granted": sorted(f"{h}:{p}" for h, p in policy.granted()),
            "tcp": sorted(f"{h}:{_ports(a, b)}" for h, a, b in policy.granted_tcp()),
            "refused": policy.recent_refusals(),
        },
        # Before "name" in the sorted output: the shell checks of the integration tests read what follows "name" up to the next brace.
        "folders": {
            "granted": [{"path": bind.dest, "mode": bind.mode} for bind in status.binds],
            "refused": dict(status.path_refusals),
        },
        # The processes and threads of its sandbox against limits.processes (counted by the gateway), and the
        # times it reported that the kernel would not create one. Both after "state": see the note on "folders".
        "tasks": None if status.tasks is None else {
            "current": status.tasks.current, "peak": status.tasks.peak, "limit": status.tasks.limit,
        },
        "task_limit_hits": {"count": status.task_failures, "last": status.task_failure},
    }


def container_view() -> dict | None:
    """The pids of the gateway's container against its limit (``pids_limit``): ``limit`` is null when none is set,
    and the whole object is null when the controller cannot be read."""
    cgroup = read_pids_cgroup()
    return None if cgroup is None else {"pids": cgroup.current, "pids_limit": cgroup.limit}


class Control:
    """The operations; separate from the socket so they can be tested without one."""

    def __init__(self, config: GatewayConfig, supervisor: Supervisor) -> None:
        self._config = config
        self._supervisor = supervisor

    def sandbox(self) -> dict:
        health = self._supervisor.health
        return {"available": health.available, "reason": health.reason}

    def handle(self, request: object) -> dict:
        if not isinstance(request, dict):
            return {"ok": False, "error": "a request is a JSON object"}
        op = request.get("op")
        supervisor = self._supervisor
        if op == "list":
            return {
                "ok": True,
                "epoch": supervisor.epoch,
                "desired": supervisor.desired_generation,
                "generation": supervisor.generation,
                "workspace_root": str(self._config.workspace_dir),
                "sandbox": self.sandbox(),
                "container": container_view(),
                "rejected": supervisor.rejected,
                "providers": [provider_view(m, self._config.run_dir) for m in supervisor.providers()],
            }
        if op == "health":
            providers = {m.name: m.status.state for m in supervisor.providers()}
            if supervisor.health.available is False:
                status = "unavailable"
            elif any(state != RUNNING for state in providers.values()):
                status = "degraded"
            else:
                status = "ok"
            reply = {"ok": True, "status": status, "version": VERSION, "sandbox": self.sandbox(), "container": container_view(), "providers": providers}
            if status == "unavailable":
                reply["detail"] = f"sandbox unavailable: {supervisor.health.reason}"
            return reply
        if op == "egress":
            managed = supervisor.get(str(request.get("provider")))
            targets, ttl = request.get("targets"), request.get("ttl")
            tcp, paths = request.get("tcp") or {}, request.get("paths") or {}
            if managed is None or managed.status.policy is None:
                return {"ok": False, "error": "unknown provider"}
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                return {"ok": False, "error": "'targets' is a list of 'host:port'"}
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
                return {"ok": False, "error": "'ttl' is a positive number of seconds"}
            if not isinstance(tcp, dict) or not all(
                isinstance(key, str) and isinstance(hosts, list) and all(isinstance(h, str) for h in hosts) for key, hosts in tcp.items()
            ):
                return {"ok": False, "error": "'tcp' maps a setting to a list of hosts"}
            if not isinstance(paths, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in paths.items()):
                return {"ok": False, "error": "'paths' maps a setting to a path"}
            report = managed.grant(targets, min(float(ttl), MAX_TTL), tcp, paths)
            return {
                "ok": True,
                "granted": report.targets,
                "paths": {"granted": [bind.dest for bind in report.binds], "refused": report.refused},
                "restarted": report.restarted,
            }
        if op == "install":
            generation = request.get("generation")
            if generation is not None and (not isinstance(generation, (str, int)) or isinstance(generation, bool) or len(str(generation)) > 128):
                return {"ok": False, "error": "'generation' is a short string or number"}
            try:
                entries, credentials, refused = parse_set(request.get("providers"))
            except DesiredError as exc:
                return {"ok": False, "error": str(exc)}
            for name, reason in refused.items():
                logger.error("desired providers: %s: %s", name, reason)
            supervisor.apply(entries, refused, credentials, None if generation is None else str(generation))
            return {"ok": True, "accepted": [entry.name for entry in entries], "rejected": refused}
        return {"ok": False, "error": f"unknown operation {op!r}"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        control: Control = self.server.control  # type: ignore[attr-defined]
        while True:
            line = self.rfile.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                reply = {"ok": False, "error": "request too long"}
            else:
                try:
                    reply = control.handle(json.loads(line))
                except ValueError:
                    reply = {"ok": False, "error": "not JSON"}
                except Exception as exc:  # noqa: BLE001 — a bad request must not stop the gateway
                    logger.exception("control request failed")
                    reply = {"ok": False, "error": str(exc)}
            self.wfile.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            self.wfile.flush()


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, control: Control) -> None:
        self.control = control
        self.socket_path = socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        super().__init__(str(socket_path), _Handler)
        socket_path.chmod(0o600)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


def _ports(first: int, last: int) -> str:
    return "any" if (first, last) == (1, 65535) else str(first) if first == last else f"{first}-{last}"
