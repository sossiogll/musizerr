"""Test doubles for the sources: catalogs served by a fake HTTP server and a gateway's control socket."""
from __future__ import annotations

import json
import socketserver
import tempfile
import threading
from pathlib import Path

import httpx

from musizeer_sdk.interfaces import PROVIDER_API_VERSION

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
TOKEN = "ghp_canary_0123456789"

LRCLIB_PERMISSIONS = {"egress": ["lrclib.net:443"], "egress_from_settings": [], "filesystem_from_settings": [], "limits": {}}


def entry(name="lrclib", digest=DIGEST_A, **kwargs) -> dict:
    return {
        "name": name, "image": f"ghcr.io/owner/musizeer-provider-{name}", "digest": digest,
        "provider_api_version": PROVIDER_API_VERSION, "roles": ["lyrics"], "label": name.upper(),
        "description": f"The {name} provider", "permissions": LRCLIB_PERMISSIONS, **kwargs,
    }


def catalog(*entries, name="Test catalog") -> dict:
    return {"name": name, "version": 2, "providers": list(entries)}


class FakeCatalogServer:
    """An ``httpx`` transport that serves catalogs from a dict of URL -> (status, headers, body), records the
    requests and can require a bearer token."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[httpx.Request] = []
        self.require_token: dict[str, str] = {}  # url -> token (the right one)
        self.transport = httpx.MockTransport(self._handle)

    def serve(self, url: str, document, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.routes[url] = (status, headers or {}, body)

    def redirect(self, url: str, to: str) -> None:
        self.routes[url] = (302, {"location": to}, b"")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        wanted = self.require_token.get(url)
        if wanted is not None and request.headers.get("authorization") != f"Bearer {wanted}":
            return httpx.Response(404 if wanted else 401, content=b"")
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"")
        status, headers, body = route
        return httpx.Response(status, headers=headers, content=body)

    def authorizations(self) -> list[tuple[str, str | None]]:
        return [(str(r.url), r.headers.get("authorization")) for r in self.requests]


class FakeControlSocket:
    """The gateway's control socket: records what the host sends and answers like the gateway."""

    def __init__(self, epoch: str = "e1") -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mzp4-gw-"))
        self.path = self.dir / "gateway.sock"
        self.requests: list[dict] = []
        self.epoch = epoch
        self.desired: str | None = None
        self.providers: list[dict] = []
        self.install_reply: dict | None = None
        self.sandbox = {"available": True, "reason": "ok"}
        self.rejected: dict[str, str] = {}
        # The root folders and the working folder the host pushed (ADR-0008), and how this gateway judges them:
        # a path in ``refused_paths`` is refused with its reason, every other root is accepted.
        self.roots: list[dict] = []
        self.workspace: str | None = None
        self.folders_pushed = False
        self.refused_paths: dict[str, str] = {}
        self.inspections: dict[str, dict] = {}
        self.browsings: dict[str | None, dict] = {}
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    request = json.loads(line)
                    fake.requests.append(request)
                    self.wfile.write(json.dumps(fake.answer(request)).encode() + b"\n")
                    self.wfile.flush()

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def answer(self, request: dict) -> dict:
        if request["op"] == "install":
            if self.install_reply is not None:
                return self.install_reply
            self.desired = request.get("generation")
            names = [p["name"] for p in request["providers"]]
            self.providers = [
                {"name": n, "state": "running", "reason": "", "socket": f"providers/{n}/provider.sock", "restarts": 0,
                 "declared": {"name": n, "roles": ["lyrics"], "provider_api_version": PROVIDER_API_VERSION, "permissions": {}}}
                for n in names
            ]
            self.roots = list(request.get("roots") or [])
            self.workspace = request.get("workspace")
            self.folders_pushed = True
            return {"ok": True, "accepted": names, "rejected": {}, "roots": self.root_reports(), "workspace": self.workspace_report()}
        if request["op"] == "list":
            return {
                "ok": True, "epoch": self.epoch, "generation": len(self.providers), "desired": self.desired,
                "workspace_root": self.workspace if self.folders_pushed else "/downloads/.musizeer-work",
                "workspace": self.workspace_report(), "roots": self.root_reports(),
                "sandbox": self.sandbox, "rejected": self.rejected, "providers": self.providers,
            }
        if request["op"] == "inspect":
            path = request["path"]
            return self.inspections.get(path) or {
                "ok": True, "path": path, "exists": True, "directory": True, "writable": True, "mounted": True, "reason": "",
                "device": "8:1", "mount_id": 5, "mount_point": path, "fs_type": "ext4", "free_bytes": 1024, "real_path": path,
                **({"write_test": {"ok": True, "error": ""}} if request.get("write_test") else {}),
            }
        if request["op"] == "browse":
            path = request.get("path")
            return self.browsings.get(path) or {
                "ok": True, "path": path, "parent": None, "volume": None, "truncated": False, "directories": [],
            }
        return {"ok": False, "error": "unknown"}

    def root_reports(self) -> list[dict]:
        return [
            {"id": root["id"], "path": root["path"], "state": "refused" if root["path"] in self.refused_paths else "accepted",
             "reason": self.refused_paths.get(root["path"], ""), "device": "8:1", "mount_id": 5, "mount_point": root["path"],
             "fs_type": "ext4", "writable": True, "free_bytes": 1024}
            for root in self.roots
        ]

    def workspace_report(self) -> dict:
        return {"path": self.workspace, "state": "accepted" if self.workspace else "unset", "reason": ""}

    def pushes(self) -> list[dict]:
        return [r for r in self.requests if r["op"] == "install"]

    def restart(self) -> None:
        """A gateway that restarted: another epoch and no desired set."""
        self.epoch += "x"
        self.desired = None
        self.providers = []

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
