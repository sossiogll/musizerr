"""A fake provider gateway, for the tests of the host's side of it: its control socket, answering
from a listing the test edits, and the sockets of the providers it lists
(``test_provider_gateway.py``, ``test_worker_registry_fork.py``)."""
from __future__ import annotations

import json
import socketserver
import threading
from pathlib import Path

import pytest

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizerr_sdk.models import Lyrics
from musizerr_sdk.permissions import Permissions, to_json
from musizerr_sdk.runner import ProviderRunner, RunnerServer

NO_AUTH = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
PERMISSIONS = Permissions(egress=("api.example.org:443",), egress_from_settings=("url",))


class Remote(LyricsProvider):
    name = "remote"
    label = "Remote"
    auth = NO_AUTH
    provider_api_version = PROVIDER_API_VERSION
    permissions = PERMISSIONS

    def validate_config(self, config): return True
    def test_connection(self, session): return None

    def get_lyrics(self, track, session):
        return Lyrics(plain=f"lyrics of {track.title} for {self.context.setting('url')}")


def declared(name="remote", roles=("lyrics",), version=PROVIDER_API_VERSION, permissions=PERMISSIONS) -> dict:
    return {"name": name, "roles": list(roles), "provider_api_version": version, "permissions": json.loads(to_json(permissions))}


class FakeGateway:
    """The control socket of a gateway, answering from a listing the test edits, and the sockets
    of the providers it lists."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.providers: list[dict] = []
        self.generation = 1
        self.epoch = "e1"
        self.requests: list[dict] = []
        self.servers: list[RunnerServer] = []
        gateway = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    request = json.loads(line)
                    gateway.requests.append(request)
                    self.wfile.write(json.dumps(gateway.answer(request)).encode() + b"\n")
                    self.wfile.flush()

        self.server = socketserver.ThreadingUnixStreamServer(str(run_dir / "gateway.sock"), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def answer(self, request: dict) -> dict:
        if request["op"] == "list":
            return {"ok": True, "epoch": self.epoch, "generation": self.generation, "workspace_root": "/downloads/.musizerr-work",
                    "sandbox": {"available": True, "reason": "ok"}, "rejected": {}, "providers": self.providers}
        if request["op"] == "egress":
            return {"ok": True, "granted": len(request["targets"])}
        return {"ok": False, "error": "unknown"}

    def serve(self, factory, name: str, *, state="running", declaration=None) -> None:
        """List a provider and serve it on its socket."""
        socket_dir = self.run_dir / "providers" / name
        socket_dir.mkdir(parents=True, exist_ok=True)
        entry = {"name": name, "image": f"r.test/m/{name}@sha256:{'a' * 64}", "state": state, "reason": "", "restarts": 0,
                 "started_at": 1.0, "socket": f"providers/{name}/provider.sock" if state == "running" else None,
                 "declared": declaration if declaration is not None else declared(name)}
        if state == "running":
            server = RunnerServer(str(socket_dir / "provider.sock"), ProviderRunner(factory))
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
            self.servers.append(server)
        self.providers.append(entry)

    def close(self) -> None:
        for server in (*self.servers, self.server):
            server.shutdown()
            server.server_close()


@pytest.fixture
def gateway(tmp_path):
    fake = FakeGateway(tmp_path)
    yield fake
    fake.close()
