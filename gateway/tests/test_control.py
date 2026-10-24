"""The control socket: what the host can ask the gateway and tell it."""
from __future__ import annotations

import json
import socket
import threading

import pytest

from musizeer_gateway import filesystem, roots, supervisor as sv
from musizeer_gateway.config import GatewayConfig
from musizeer_gateway.control import Control, ControlServer
from musizeer_gateway.egress import EgressPolicy
from musizeer_gateway.desired import DesiredProvider
from musizeer_sdk.interfaces import PROVIDER_API_VERSION
from musizeer_sdk.labels import DeclaredManifest
from musizeer_gateway.filesystem import Bind
from musizeer_sdk.permissions import Permissions, TcpAccess


class Managed:
    """Enough of a ManagedProvider for the control operations."""

    def __init__(self, config, name, state, reason=""):
        entry = DesiredProvider(name, "r.test", f"m/{name}", "sha256:" + "a" * 64, ("lyrics",), Permissions(egress=("lrclib.net:443",)))
        self.status = sv.ProviderStatus(entry, state, reason, declared=DeclaredManifest(
            name, ("lyrics",), PROVIDER_API_VERSION, Permissions(egress=("lrclib.net:443",)),
        ), policy=EgressPolicy(name, ["lrclib.net:443"]))
        self.name = name
        self.socket_path = config.providers_dir / name / "provider.sock"
        self.granted_paths = None

    def grant(self, targets, ttl, tcp=None, paths=None):
        count = self.status.policy.grant(targets, ttl, tcp)
        self.granted_paths = paths
        return sv.GrantReport(count, (), {}, False)


class FakeSupervisor:
    epoch, generation, rejected = "e1", 4, {"bad": "no digest"}
    desired_generation = "g7"

    def __init__(self, config, managed, available=True, reason="ok"):
        self.health = type("H", (), {"available": available, "reason": reason})()
        self._managed = managed
        self.applied = []
        self._declared = None
        self._mounts = ()

    def apply(self, entries, refused, credentials, generation, folders=None):
        self.applied.append((entries, refused, credentials, generation))
        self.folders_applied = folders
        self._declared = folders

    @property
    def own_folders(self):
        return ("/var/lib/musizeer-gateway", "/run/musizeer")

    def mounts(self):
        return self._mounts

    def folders(self):
        return roots.evaluate(self._declared or roots.DeclaredFolders(), self._mounts, self.own_folders)

    def providers(self):
        return self._managed

    def get(self, name):
        return next((m for m in self._managed if m.name == name), None)


@pytest.fixture(autouse=True)
def no_reserved(monkeypatch):
    # pytest's temporary folders live under /tmp, which a sandbox mounts itself (a root there is refused)
    monkeypatch.setattr(filesystem, "RESERVED", ())


@pytest.fixture
def config(tmp_path):
    return GatewayConfig(state_dir=tmp_path / "s", run_dir=tmp_path / "run")


def control(config, managed, **kwargs):
    supervisor = FakeSupervisor(config, managed, **kwargs)
    return Control(config, supervisor), supervisor


def test_list_gives_each_provider_its_state_manifest_socket_and_egress(config):
    running, down = Managed(config, "lrclib", sv.RUNNING), Managed(config, "broken", sv.REFUSED, "labels disagree")
    running.status.policy.grant(["navidrome.lan:4533"], 60)
    running.status.policy.refuse("evil.example:443", "not a declared destination")
    operations, _ = control(config, [running, down])

    reply = operations.handle({"op": "list"})

    assert reply["ok"] and (reply["epoch"], reply["generation"], reply["desired"]) == ("e1", 4, "g7")
    assert reply["workspace_root"] is None and reply["rejected"] == {"bad": "no digest"}
    assert reply["workspace"] == {"path": None, "state": "unset", "reason": ""} and reply["roots"] == []
    assert reply["sandbox"] == {"available": True, "reason": "ok"}
    first, second = reply["providers"]
    assert first["name"] == "lrclib" and first["state"] == "running" and first["socket"] == "providers/lrclib/provider.sock"
    assert first["declared"]["roles"] == ["lyrics"] and first["declared"]["permissions"]["egress"] == ["lrclib.net:443"]
    assert first["egress"]["fixed"] == ["lrclib.net:443"] and first["egress"]["granted"] == ["navidrome.lan:4533"]
    assert first["egress"]["refused"][0]["target"] == "evil.example:443"
    assert second["state"] == "refused" and second["reason"] == "labels disagree" and second["socket"] is None
    json.dumps(reply)


def test_health_says_ok_degraded_or_unavailable_with_the_reason(config):
    ok, _ = control(config, [Managed(config, "a", sv.RUNNING)])
    degraded, _ = control(config, [Managed(config, "a", sv.RUNNING), Managed(config, "b", sv.RESTARTING)])
    closed, _ = control(config, [Managed(config, "a", sv.UNAVAILABLE)], available=False, reason="bwrap: No permissions to create a new namespace")

    assert ok.handle({"op": "health"})["status"] == "ok"
    assert degraded.handle({"op": "health"})["status"] == "degraded"
    reply = closed.handle({"op": "health"})
    assert reply["status"] == "unavailable" and reply["sandbox"]["reason"].startswith("bwrap: No permissions")
    assert reply["detail"] == "sandbox unavailable: bwrap: No permissions to create a new namespace"


def test_the_host_tells_the_gateway_the_destinations_the_settings_name(config):
    managed = Managed(config, "navidrome", sv.RUNNING)
    operations, _ = control(config, [managed])

    reply = operations.handle({"op": "egress", "provider": "navidrome", "targets": ["music.lan:4533", "junk"], "ttl": 120})

    assert reply == {"ok": True, "granted": 1, "paths": {"granted": [], "refused": {}}, "restarted": False}
    assert managed.status.policy.decide("music.lan", 4533).allowed


@pytest.mark.parametrize("request_, error", [
    ({"op": "egress", "provider": "ghost", "targets": [], "ttl": 1}, "unknown provider"),
    ({"op": "egress", "provider": "navidrome", "targets": "x", "ttl": 1}, "list"),
    ({"op": "egress", "provider": "navidrome", "targets": [1], "ttl": 1}, "list"),
    ({"op": "egress", "provider": "navidrome", "targets": [], "ttl": 0}, "ttl"),
    ({"op": "egress", "provider": "navidrome", "targets": [], "ttl": True}, "ttl"),
    ({"op": "nope"}, "unknown operation"),
    ([], "JSON object"),
])
def test_a_request_that_makes_no_sense_is_answered_with_an_error(config, request_, error):
    operations, _ = control(config, [Managed(config, "navidrome", sv.RUNNING)])

    reply = operations.handle(request_)

    assert reply["ok"] is False and error in reply["error"]


def test_a_grant_is_capped_in_time(config):
    managed = Managed(config, "navidrome", sv.RUNNING)
    operations, _ = control(config, [managed])
    operations.handle({"op": "egress", "provider": "navidrome", "targets": ["a.lan:1"], "ttl": 10**9})

    assert managed.status.policy._expires - managed.status.policy._clock() <= 3600.5  # noqa: SLF001


D = "sha256:" + "a" * 64


def pushed(name="lrclib", **kwargs):
    return {
        "name": name, "image": f"ghcr.io/sossiogll/musizeer-provider-{name}", "digest": D, "roles": ["lyrics"],
        "permissions": {"egress": ["lrclib.net:443"]}, **kwargs,
    }


def test_the_host_pushes_the_desired_set_and_the_supervisor_applies_it(config):
    operations, supervisor = control(config, [])

    reply = operations.handle({"op": "install", "generation": "abc", "providers": [pushed(credential="tok"), pushed("bad", digest="main")]})

    assert reply["ok"] and reply["accepted"] == ["lrclib"] and set(reply["rejected"]) == {"bad"}
    entries, refused, credentials, generation = supervisor.applied[0]
    assert [e.name for e in entries] == ["lrclib"] and credentials == {"lrclib": "tok"} and generation == "abc"
    assert "tok" not in json.dumps(reply)


def test_an_empty_set_removes_everything_and_a_malformed_one_changes_nothing(config):
    operations, supervisor = control(config, [])

    assert operations.handle({"op": "install", "providers": []})["ok"]
    assert supervisor.applied[-1][0] == []
    for request_ in (
        {"op": "install"}, {"op": "install", "providers": {"a": 1}},
        {"op": "install", "providers": [], "generation": ["x"]}, {"op": "install", "providers": [], "generation": True},
    ):
        before = len(supervisor.applied)
        assert operations.handle(request_)["ok"] is False
        assert len(supervisor.applied) == before


def test_there_is_no_reload_any_more(config):
    operations, _ = control(config, [])

    assert operations.handle({"op": "reload"})["ok"] is False


def test_the_socket_speaks_one_json_line_per_request_and_is_the_owners_alone(config, tmp_path):
    operations, _ = control(config, [Managed(config, "a", sv.RUNNING)])
    path = tmp_path / "gateway.sock"
    server = ControlServer(path, operations)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(path))
            stream = client.makefile("rwb")
            stream.write(b'{"op": "health"}\nnot json\n{"op": "nope"}\n')
            stream.flush()
            replies = [json.loads(stream.readline()) for _ in range(3)]
    finally:
        server.shutdown()
        server.server_close()

    assert replies[0]["status"] == "ok"
    assert replies[1] == {"ok": False, "error": "not JSON"}
    assert replies[2]["ok"] is False
    assert not path.exists()


def test_the_host_grants_raw_tcp_hosts_and_folders_per_setting(config):
    managed = Managed(config, "files", sv.RUNNING)
    managed.status.policy = EgressPolicy("files", [], tcp=[TcpAccess("host", "any")])
    operations, _ = control(config, [managed])

    reply = operations.handle({
        "op": "egress", "provider": "files", "targets": [], "ttl": 120,
        "tcp": {"host": ["ftp.lan"], "other": ["x.lan"]}, "paths": {"root_dir": "/downloads/music"},
    })

    assert reply["ok"] and managed.granted_paths == {"root_dir": "/downloads/music"}
    assert managed.status.policy.decide("ftp.lan", 40000).allowed, "any port of the host the setting names"
    assert not managed.status.policy.decide("x.lan", 21).allowed, "a setting the manifest does not declare grants nothing"


@pytest.mark.parametrize("extra", [{"tcp": "x"}, {"tcp": {"host": "ftp.lan"}}, {"tcp": {"host": [1]}}, {"paths": ["/a"]}, {"paths": {"root_dir": 5}}])
def test_a_malformed_tcp_or_paths_grant_is_an_error(config, extra):
    managed = Managed(config, "files", sv.RUNNING)
    operations, _ = control(config, [managed])

    reply = operations.handle({"op": "egress", "provider": "files", "targets": [], "ttl": 1, **extra})

    assert reply["ok"] is False and managed.granted_paths is None


def test_list_shows_the_wildcards_the_raw_tcp_grants_and_the_folders(config, tmp_path):
    managed = Managed(config, "files", sv.RUNNING)
    managed.status.policy = EgressPolicy("files", ["*.cdn.example:443"], tcp=[TcpAccess("host", "30000-30100")])
    managed.status.policy.grant([], 60, {"host": ["ftp.lan"]})
    managed.status.binds = (Bind(source="/real/music", dest="/downloads/music", mode="rw"),)
    managed.status.path_refusals = {"cache": "is not a directory"}
    operations, _ = control(config, [managed])

    provider = operations.handle({"op": "list"})["providers"][0]

    assert provider["egress"]["fixed"] == ["*.cdn.example:443"] and provider["egress"]["tcp"] == ["ftp.lan:30000-30100"]
    assert provider["folders"] == {"granted": [{"path": "/downloads/music", "mode": "rw"}], "refused": {"cache": "is not a directory"}}


# ------------------------------------------------------------------
# Root folders and the working folder (ADR-0008)
# ------------------------------------------------------------------


def fake_mounts(*points):
    table = [roots.Mount(1, 0, "0:1", "/", "/", "overlay")]
    table += [roots.Mount(10 + i, 1, f"8:{i}", "/", point, "ext4") for i, point in enumerate(points)]
    return tuple(table)


def test_the_host_pushes_the_root_folders_and_the_working_folder_with_the_set(config, tmp_path):
    library = tmp_path / "lib"
    (library / "work").mkdir(parents=True)
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts(str(library))  # noqa: SLF001

    reply = operations.handle({
        "op": "install", "providers": [], "generation": "g",
        "roots": [{"id": "r1", "path": str(library)}, {"id": "r2", "path": "/etc"}], "workspace": str(library / "work"),
    })

    assert reply["ok"]
    declared = supervisor.folders_applied
    assert [(r.id, r.path) for r in declared.roots] == [("r1", str(library)), ("r2", "/etc")] and declared.workspace == str(library / "work")
    by_id = {r["id"]: r for r in reply["roots"]}
    assert by_id["r1"]["state"] == "accepted" and by_id["r2"]["state"] == "refused"
    assert reply["workspace"] == {"path": str(library / "work"), "state": "accepted", "reason": ""}


def test_list_reports_what_the_gateway_made_of_each_root_and_of_the_working_folder(config, tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts(str(library))  # noqa: SLF001
    operations.handle({
        "op": "install", "providers": [], "roots": [{"id": "lib", "path": str(library)}, {"id": "bad", "path": str(tmp_path / "missing")}],
        "workspace": str(tmp_path / "elsewhere"),
    })

    reply = operations.handle({"op": "list"})

    lib, bad = reply["roots"]
    assert (lib["id"], lib["state"], lib["writable"], lib["device"], lib["mount_point"]) == ("lib", "accepted", True, "8:0", str(library))
    assert isinstance(lib["free_bytes"], int) and isinstance(lib["mount_id"], int)
    assert bad["state"] == "refused" and "cannot be read" in bad["reason"]
    assert reply["workspace_root"] is None
    assert reply["workspace"]["state"] == "refused" and "not inside an accepted root folder" in reply["workspace"]["reason"]


@pytest.mark.parametrize("extra", [
    {"roots": "x"}, {"roots": [1]}, {"roots": [{"id": "a"}]}, {"roots": [{"id": "a", "path": 5}]},
    {"roots": [{"id": "a b", "path": "/x"}]}, {"roots": [{"id": "a", "path": "/x"}, {"id": "a", "path": "/y"}]},
    {"workspace": ""}, {"workspace": 5},
])
def test_roots_that_are_not_roots_change_nothing(config, extra):
    operations, supervisor = control(config, [])

    reply = operations.handle({"op": "install", "providers": [], **extra})

    assert reply["ok"] is False and supervisor.applied == []


def test_an_install_without_folders_declares_none(config):
    operations, supervisor = control(config, [])

    assert operations.handle({"op": "install", "providers": []})["ok"]

    assert supervisor.folders_applied == roots.DeclaredFolders()


def test_inspect_says_what_is_at_a_path_and_nothing_of_its_content(config, tmp_path):
    library = tmp_path / "lib"
    (library / "sub").mkdir(parents=True)
    (library / "secret.txt").write_text("hidden")
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts(str(library))  # noqa: SLF001

    reply = operations.handle({"op": "inspect", "path": str(library / "sub")})

    assert reply["ok"] and reply["exists"] and reply["directory"] and reply["writable"] and reply["mounted"]
    assert reply["mount_point"] == str(library) and reply["device"] == "8:0" and reply["reason"] == ""
    assert "hidden" not in json.dumps(reply) and "secret.txt" not in json.dumps(reply)
    outside = operations.handle({"op": "inspect", "path": str(tmp_path)})
    assert outside["mounted"] is False and "container's own file system" in outside["reason"]
    assert operations.handle({"op": "inspect", "path": str(library / "nope")})["exists"] is False
    assert operations.handle({"op": "inspect", "path": str(library / "secret.txt")})["reason"] == "is not a directory"


@pytest.mark.parametrize("path", [None, "", "relative", "/a/../b", "/a//b", "/a/", "a\0b", 5])
def test_inspect_refuses_a_path_that_is_not_absolute_and_normalized(config, path):
    operations, _ = control(config, [])

    reply = operations.handle({"op": "inspect", "path": path})

    assert reply["ok"] is False


def test_browse_lists_the_mounted_volumes_without_a_path(config, tmp_path):
    library, downloads = tmp_path / "lib", tmp_path / "dl"
    library.mkdir()
    downloads.mkdir()
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts(str(library), str(downloads)) + (  # noqa: SLF001
        roots.Mount(90, 1, "0:90", "/", "/proc", "proc"), roots.Mount(91, 1, "0:91", "/", "/dev/shm", "tmpfs"),
        roots.Mount(92, 1, "8:9", "/hosts", "/etc/hosts", "ext4"),  # a file bind-mounted by Docker
        roots.Mount(93, 1, "8:2", "/", str(config.run_dir), "ext4"),  # the gateway's own run directory
    )

    for request_ in ({"op": "browse"}, {"op": "browse", "path": None}, {"op": "browse", "path": ""}):
        reply = operations.handle(request_)
        assert reply["ok"] and reply["path"] is None
        assert [d["path"] for d in reply["directories"]] == sorted([str(library), str(downloads)])


def test_browse_lists_only_the_subdirectories_of_a_path_inside_a_volume(config, tmp_path):
    library = tmp_path / "lib"
    (library / "Rock").mkdir(parents=True)
    (library / ".hidden").mkdir()
    (library / "a-file.flac").write_text("x")
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts(str(library))  # noqa: SLF001

    reply = operations.handle({"op": "browse", "path": str(library)})

    assert reply["ok"] and [d["name"] for d in reply["directories"]] == [".hidden", "Rock"]
    assert reply["parent"] is None and reply["volume"]["mount_point"] == str(library)
    inner = operations.handle({"op": "browse", "path": str(library / "Rock")})
    assert inner["directories"] == [] and inner["parent"] == str(library)


def test_browse_refuses_a_path_that_is_not_in_a_mounted_volume(config, tmp_path):
    operations, supervisor = control(config, [])
    supervisor._mounts = fake_mounts()  # noqa: SLF001

    reply = operations.handle({"op": "browse", "path": str(tmp_path)})

    assert reply["ok"] is False and "not a mounted volume" in reply["error"]
