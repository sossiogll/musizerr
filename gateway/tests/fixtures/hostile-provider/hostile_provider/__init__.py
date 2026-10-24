"""
TEST FIXTURE. A lyrics provider that, when asked for lyrics, attacks the sandbox it runs in and
returns what happened as JSON in ``Lyrics.plain``: for each attempt, whether it was BLOCKED (``ok``). A
``control`` is something the sandbox must allow (it proves the attack code works and that the
legitimate path is open): there ``ok`` means it worked. ``tests/integration/check_hostile_report.py`` asserts on the report.

It declares ONE destination, lrclib.net:443, and a setting-named one that nobody configures.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
from pathlib import Path

from musizeer_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizeer_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizeer_sdk.models import Lyrics
from musizeer_sdk.permissions import Permissions

CANARY = "hostile-should-never-see-this"


def attempt(report: list, name: str, blocked_if):
    """An attack. ``blocked_if()`` returns (blocked, detail) or raises (an error is a block, and its
    text the detail)."""
    try:
        blocked, detail = blocked_if()
    except Exception as exc:  # noqa: BLE001
        blocked, detail = True, f"{type(exc).__name__}: {exc}"
    report.append({"name": name, "ok": bool(blocked), "control": False, "detail": str(detail)[:200]})


def control(report: list, name: str, works):
    """Something the sandbox MUST allow. ``works()`` returns (worked, detail) or raises (it did not)."""
    try:
        worked, detail = works()
    except Exception as exc:  # noqa: BLE001
        worked, detail = False, f"{type(exc).__name__}: {exc}"
    report.append({"name": name, "ok": bool(worked), "control": True, "detail": str(detail)[:200]})


def connect_via_proxy(target: str) -> str:
    """The status line the egress proxy answers a CONNECT to ``target`` with."""
    proxy = os.environ["HTTPS_PROXY"].split("//", 1)[1]
    host, port = proxy.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=10) as connection:
        connection.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return connection.recv(200).split(b"\r\n", 1)[0].decode()


def refused(target: str):
    status = connect_via_proxy(target)
    return " 403 " in status or " 400 " in status or " 502 " in status, status


def syscall(number_by_arch: dict[str, int], *args: int) -> tuple[int, int]:
    import platform

    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(number_by_arch[platform.machine()]), *[ctypes.c_long(a) for a in args])
    return result, ctypes.get_errno()


def run() -> list:
    report: list = []

    # ---- the file system ----------------------------------------------------------------
    def host_paths():
        found = [p for p in ("/data", "/downloads", "/var/lib/musizeer-gateway", "/etc/musizeer-gateway", "/gateway", "/sdk",
                             "/run/musizeer", "/Users", "/var/run/docker.sock") if os.path.exists(p)]
        return not found, f"visible: {found}"

    attempt(report, "the host's data, library, gateway state and docker socket are not there", host_paths)
    attempt(report, "reading the database file under /data", lambda: (not Path("/data/musizeer.db").exists(), "exists" if Path("/data/musizeer.db").exists() else "absent"))

    def pid1_environ():
        content = Path("/proc/1/environ").read_bytes().decode(errors="replace")
        return CANARY not in content and "POSTGRES_PASSWORD" not in content and "AUTH_SECRET" not in content, f"{len(content)} bytes, secrets: {CANARY in content}"

    attempt(report, "/proc/1/environ holds nothing of the host", pid1_environ)

    def own_environment():
        keys = sorted(os.environ)
        bad = [k for k in keys if k in ("POSTGRES_PASSWORD", "MUSIZEER_AUTH_SECRET", "MUSIZEER_CREDENTIALS_KEY", "GATEWAY_CANARY", "REDIS_URL", "DATABASE_URL")]
        return not bad and CANARY not in "".join(os.environ.values()), f"variables: {keys}"

    attempt(report, "the environment holds nothing of the host's", own_environment)

    def walk_out():
        parent = Path("/workspace/..").resolve()
        entries = sorted(os.listdir("/workspace/.."))
        return entries == sorted(os.listdir("/")) and not Path("/workspace/../lrclib").exists() and "canary.txt" not in entries, f"/workspace/.. is {parent}"

    attempt(report, "/workspace/.. is the sandbox's own root, not the host's workspace directory", walk_out)

    def other_workspace():
        return not any(Path(p).exists() for p in ("/workspace/../lrclib", "/downloads/.musizeer-work", "/workspace/canary.txt")), "invisible"

    attempt(report, "another provider's workspace and the host's files are invisible", other_workspace)

    def write_root():
        Path("/injected").write_text("x")
        return False, "wrote /injected"

    attempt(report, "writing to the read-only root", write_root)

    def write_usr():
        Path("/usr/local/bin/injected").write_text("x")
        return False, "wrote into /usr"

    attempt(report, "writing into the image's /usr", write_usr)

    def replace_egress_socket():
        os.unlink("/run/egress/egress.sock")
        return False, "removed the egress socket"

    attempt(report, "removing the egress proxy's socket", replace_egress_socket)

    def write_to(path: str):
        Path(path).write_text("ok")
        return True, f"wrote {path}"

    control(report, "its own /tmp is writable", lambda: write_to("/tmp/scratch"))
    control(report, "its own workspace folder is writable", lambda: write_to("/workspace/hostile-was-here"))

    # ---- the network ---------------------------------------------------------------------
    def direct_ip():
        socket.create_connection(("1.1.1.1", 443), timeout=4).close()
        return False, "connected to 1.1.1.1:443 without the proxy"

    attempt(report, "connecting to an IP address directly, without the proxy", direct_ip)

    def direct_lan():
        socket.create_connection(("192.168.0.1", 80), timeout=4).close()
        return False, "connected to a LAN address"

    attempt(report, "connecting to a LAN address directly", direct_lan)

    def dns():
        socket.getaddrinfo("example.com", 443)
        return False, "resolved example.com"

    attempt(report, "resolving a name (there is no resolver in the sandbox)", dns)
    attempt(report, "the proxy refuses an undeclared host (example.com:443)", lambda: refused("example.com:443"))
    attempt(report, "the proxy refuses the declared host on another port (lrclib.net:22)", lambda: refused("lrclib.net:22"))
    attempt(report, "the proxy refuses a private address (10.0.0.1:80)", lambda: refused("10.0.0.1:80"))
    attempt(report, "the proxy refuses the cloud metadata address (169.254.169.254:80)", lambda: refused("169.254.169.254:80"))
    attempt(report, "the proxy refuses the gateway's own loopback (127.0.0.1:80)", lambda: refused("127.0.0.1:80"))
    control(report, "the proxy lets the declared destination through (lrclib.net:443)",
            lambda: (lambda status: ("200" in status, status))(connect_via_proxy("lrclib.net:443")))

    # ---- privilege -----------------------------------------------------------------------
    CLONE_NEWUSER = 0x10000000
    attempt(report, "unshare(CLONE_NEWUSER): no nested user namespace",
            lambda: ((lambda r: (r[0] == -1 and r[1] == 1, f"result {r[0]}, errno {r[1]}"))(syscall({"x86_64": 272, "aarch64": 97}, CLONE_NEWUSER))))
    attempt(report, "mount(2) is refused",
            lambda: ((lambda r: (r[0] == -1 and r[1] == 1, f"result {r[0]}, errno {r[1]}"))(syscall({"x86_64": 165, "aarch64": 40}, 0, 0, 0, 0, 0))))
    attempt(report, "setns(2) is refused",
            lambda: ((lambda r: (r[0] == -1, f"result {r[0]}, errno {r[1]}"))(syscall({"x86_64": 308, "aarch64": 268}, -1, 0))))
    attempt(report, "it is not root, and has no capability",
            lambda: (os.getuid() != 0 and "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text(), f"uid {os.getuid()}"))
    return report


class HostileLyricsProvider(LyricsProvider):
    name = "hostile"
    label = "Hostile (test fixture)"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    provider_api_version = PROVIDER_API_VERSION
    permissions = Permissions(egress=("lrclib.net:443",), egress_from_settings=("server_url",))

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        if track.title.startswith("connect:"):  # one CONNECT through the proxy, to see what it answers
            return Lyrics(plain=connect_via_proxy(track.title[len("connect:"):]))
        return Lyrics(plain=json.dumps(run()))
