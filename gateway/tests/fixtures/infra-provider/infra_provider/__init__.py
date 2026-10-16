"""
TEST FIXTURE. A lyrics provider that uses what the gateway grants besides fixed hosts, and returns what happened
as text in ``Lyrics.plain`` (the title of the track is the command):

- ``connect:host:port``  one TCP connection through the egress proxy (``musizerr_sdk.net``): ``OK`` or the error;
- ``fs``                 JSON: which folders its settings name are really there (and what the probe paths hold: an unbound mount point is an empty folder), whether they can be written,
                         how they are mounted, and an id of its process (a change of mounts is a new process);
- ``ftp:<setting>``      list and download a file with ``ftplib`` from the host the setting ``<setting>`` holds,
                         control and passive data connections through the proxy: JSON, or the error.
"""
from __future__ import annotations

import ftplib
import json
import os
import socket
import uuid
from pathlib import Path

from musizerr_sdk import net
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizerr_sdk.models import Lyrics
from musizerr_sdk.permissions import FilesystemAccess, Permissions, TcpAccess

net.route_module(ftplib)

# The sandbox has a PID namespace of its own (the pid is always the same): a new process is told by this.
INSTANCE = uuid.uuid4().hex
PROBES = ("/downloads/lib/rw", "/downloads/lib/rw2", "/downloads/lib/ro", "/downloads/lib/escape")


def mount_options(path: str) -> str:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == path:
            return fields[5]
    return "not a mount point"


def folder_report(path: str) -> dict:
    info: dict = {"path": path, "is_dir": os.path.isdir(path), "mount": mount_options(path)}
    if info["is_dir"]:
        info["listing"] = sorted(os.listdir(path))[:20]
        try:
            Path(path, "infra-was-here").write_text("written from the sandbox")
            info["write"] = "ok"
        except OSError as exc:
            info["write"] = f"{type(exc).__name__}: {exc.strerror}"
    return info


def ftp_report(host: str) -> dict:
    ftp = ftplib.FTP()
    ftp.connect(host, 2121, timeout=10)
    ftp.login("anonymous", "infra@example.test")
    names = ftp.nlst()
    chunks: list[bytes] = []
    ftp.retrbinary("RETR hello.txt", chunks.append)
    ftp.quit()
    return {"names": names, "content": b"".join(chunks).decode()}


class InfraLyricsProvider(LyricsProvider):
    name = "infra"
    label = "Infrastructure (test fixture)"
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)
    provider_api_version = PROVIDER_API_VERSION
    permissions = Permissions(
        egress=("*.example.com:443",),
        filesystem_from_settings=(FilesystemAccess("rw_dir", "rw"), FilesystemAccess("ro_dir", "ro")),
        tcp_from_settings=(TcpAccess("ftp_host", "any"), TcpAccess("ftp_ctl", "2121")),
    )

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session):
        return None

    def get_lyrics(self, track, session):
        command = track.title
        settings = self.context.settings()
        if command.startswith("connect:"):
            host, _, port = command[len("connect:"):].rpartition(":")
            try:
                net.create_connection((host, int(port)), timeout=10).close()
                return Lyrics(plain="OK")
            except OSError as exc:
                return Lyrics(plain=f"{type(exc).__name__}: {exc}")
        if command == "fs":
            report = {
                "instance": INSTANCE,
                "dirs": {key: folder_report(settings[key]) for key in ("rw_dir", "ro_dir") if settings.get(key)},
                "probes": {path: sorted(os.listdir(path)) if os.path.isdir(path) else None for path in PROBES},
                "work": sorted(os.listdir("/downloads/.musizerr-work")) if os.path.isdir("/downloads/.musizerr-work") else None,
                "downloads": sorted(os.listdir("/downloads")) if os.path.isdir("/downloads") else None,
            }
            return Lyrics(plain=json.dumps(report))
        if command.startswith("ftp:"):
            key = command[len("ftp:"):]
            try:
                return Lyrics(plain=json.dumps(ftp_report(settings[key])))
            except (OSError, KeyError, ValueError, ftplib.Error) as exc:
                return Lyrics(plain=f"{type(exc).__name__}: {exc}")
        return Lyrics(plain=socket.gethostname())
