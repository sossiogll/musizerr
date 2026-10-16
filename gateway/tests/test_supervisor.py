"""The life of a provider process: start, restart, refusal, and failing closed."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from musizerr_gateway import filesystem, supervisor as sv
from musizerr_gateway.config import GatewayConfig
from musizerr_gateway.images import PreparedImage
from musizerr_gateway.desired import DesiredProvider
from musizerr_gateway.filesystem import Bind
from musizerr_gateway.oci import ImageConfig, ImageError, RegistryError
from musizerr_gateway.sandbox import SandboxUnavailable
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import DeclaredManifest
from musizerr_sdk.permissions import FilesystemAccess, Permissions

PERMISSIONS = Permissions(egress=("lrclib.net:443",))


def desired(name="demo", digest="a"):
    return DesiredProvider(name, "registry.test", f"musizerr/{name}", "sha256:" + digest * 64, ("lyrics",), PERMISSIONS)


ENTRY = desired()

# What stands in for the sandboxed runner: it binds the socket the gateway waits for.
FAKE_RUNNER = """
import os, socket, sys, time
path = sys.argv[1]
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(5)
open(path + ".pid", "w").write(str(os.getpid()))
time.sleep(float(sys.argv[2]))
"""


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Images:
    def __init__(self, declared=None, error=None, rootfs=Path("/rootfs")):
        self.rootfs = rootfs
        self.declared = declared or DeclaredManifest("demo", ("lyrics",), PROVIDER_API_VERSION, Permissions(egress=("lrclib.net:443",)))
        self.error = error
        self.calls = 0
        self.credentials = []

    def prepare(self, entry, credential=None):
        self.calls += 1
        self.credentials.append(credential)
        if self.error:
            raise self.error
        return PreparedImage(entry, self.rootfs, ImageConfig(entrypoint=("runner",)), self.declared)


@pytest.fixture
def world(tmp_path, monkeypatch):
    config = GatewayConfig(
        state_dir=tmp_path / "state", run_dir=tmp_path / "run", workspace_dir=tmp_path / "lib" / ".work",
        filesystem_roots=(str(tmp_path / "lib"),),
    )
    monkeypatch.setattr(filesystem, "RESERVED", ())  # pytest's temporary folders live under /tmp, which a sandbox mounts itself
    built = []

    def fake_command(spec, fd):
        built.append(spec)
        return [sys.executable, "-c", FAKE_RUNNER, str(spec.socket_dir / "provider.sock"), str(lifetime[0])]

    lifetime = [60]
    monkeypatch.setattr(sv, "build_command", fake_command)
    monkeypatch.setattr(sv, "BACKOFF_START", 0.05)
    monkeypatch.setattr(sv, "READY_TIMEOUT", 5.0)
    monkeypatch.setattr(sv, "SANDBOX_RECHECK", 0.1)
    supervisors = []

    def make(images=None, probe=lambda uid, gid: None):
        health = sv.SandboxHealth(config.sandbox_uid, config.sandbox_gid, check=probe, recheck=0.1)
        supervisor = sv.Supervisor(config, images or Images(), health)
        supervisors.append(supervisor)
        return supervisor

    make.config, make.built, make.lifetime = config, built, lifetime
    yield make
    for supervisor in supervisors:
        supervisor.stop()


def state_of(supervisor, name="demo"):
    managed = supervisor.get(name)
    return managed.status.state if managed else None


def test_a_provider_is_started_and_reported_running_with_its_socket(world):
    supervisor = world()
    generation = supervisor.generation

    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    managed = supervisor.get("demo")
    assert managed.socket_path.exists() and managed.status.declared.name == "demo"
    assert supervisor.generation > generation, "the host is told the set of running providers changed"
    spec = world.built[0]
    assert spec.uid == world.config.sandbox_uid and spec.workspace_dir == world.config.workspace_dir / "demo"
    assert spec.command[-4:] == ("--socket", "/run/provider/provider.sock", "--egress-socket", "/run/egress/egress.sock")
    assert oct(spec.socket_dir.stat().st_mode & 0o777) == "0o700", "only the owner may list the sockets"
    assert (world.config.workspace_dir / "demo").is_dir()


def test_a_provider_that_dies_is_restarted(world):
    supervisor = world()
    supervisor.apply([ENTRY], {})
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    managed = supervisor.get("demo")
    pid_file = Path(str(managed.socket_path) + ".pid")
    first = int(pid_file.read_text())
    generation = supervisor.generation

    os.kill(first, signal.SIGKILL)

    assert wait_for(lambda: managed.status.restarts >= 1)
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING and int(pid_file.read_text()) != first)
    assert supervisor.generation >= generation + 2, "down and up again are both announced"


def test_a_provider_that_keeps_dying_is_restarted_with_a_growing_pause(world):
    world.lifetime[0] = 0.01
    supervisor = world()
    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: supervisor.get("demo").status.restarts >= 3)


def test_an_image_that_is_not_what_it_was_pinned_to_is_refused_and_never_run(world):
    supervisor = world(Images(error=ImageError("the manifest hashes to sha256:00, not to the pinned digest")))

    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: state_of(supervisor) == sv.REFUSED)
    assert "pinned digest" in supervisor.get("demo").status.reason
    assert world.built == []


def test_a_registry_that_cannot_be_reached_is_tried_again(world):
    images = Images(error=RegistryError("registry.test: connection refused"))
    supervisor = world(images)
    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: images.calls >= 2)
    assert state_of(supervisor) == sv.FAILED and "registry" in supervisor.get("demo").status.reason
    images.error = None
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)


def test_without_a_sandbox_the_provider_does_not_run_and_says_why(world):
    def no_sandbox(uid, gid):
        raise SandboxUnavailable("bwrap: No permissions to create a new namespace")

    supervisor = world(probe=no_sandbox)
    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: state_of(supervisor) == sv.UNAVAILABLE)
    status = supervisor.get("demo").status
    assert status.reason == "sandbox unavailable: bwrap: No permissions to create a new namespace"
    assert world.built == [], "nothing was started: there is no unconfined fallback"
    assert supervisor.health.available is False


def test_the_sandbox_is_checked_again_and_the_provider_starts_when_it_works(world):
    available = [False]

    def probe(uid, gid):
        if not available[0]:
            raise SandboxUnavailable("not yet")

    supervisor = world(probe=probe)
    supervisor.apply([ENTRY], {})
    assert wait_for(lambda: state_of(supervisor) == sv.UNAVAILABLE)

    available[0] = True

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)


def test_the_running_set_follows_the_desired_set(world):
    other = desired("other", "b")
    supervisor = world()

    supervisor.apply([ENTRY, other], {})
    assert wait_for(lambda: {state_of(supervisor, "demo"), state_of(supervisor, "other")} == {sv.RUNNING})

    supervisor.apply([other], {"broken": "no digest"})
    assert supervisor.get("demo") is None and state_of(supervisor, "other") == sv.RUNNING
    assert supervisor.rejected == {"broken": "no digest"}

    repinned = desired("other", "c")
    supervisor.apply([repinned], {})
    assert supervisor.get("other").status.entry == repinned


def test_stopping_ends_the_processes(world):
    supervisor = world()
    supervisor.apply([ENTRY], {})
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    pid = int(Path(str(supervisor.get("demo").socket_path) + ".pid").read_text())

    supervisor.stop()

    assert wait_for(lambda: not _alive(pid))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead
        return "Z" not in Path(f"/proc/{pid}/status").read_text().split("State:")[1].split("\n")[0]
    except OSError:
        return False


def test_the_sandbox_is_started_with_none_of_the_gateways_environment(world, monkeypatch):
    # /proc/1/environ of a sandbox is the environment bubblewrap started with: it must be empty.
    environments = []
    real = sv.subprocess.Popen

    def recording(command, **kwargs):
        environments.append(kwargs.get("env"))
        return real(command, **kwargs)

    monkeypatch.setattr(sv.subprocess, "Popen", recording)
    monkeypatch.setenv("GATEWAY_CANARY", "secret")
    supervisor = world()
    supervisor.apply([ENTRY], {})

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    assert environments == [{}]


def test_the_credential_is_used_to_pull_and_forgotten_once_the_image_is_unpacked(world):
    images = Images()
    supervisor = world(images)

    supervisor.apply([ENTRY], {}, {"demo": "tok"}, "g1")

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    managed = supervisor.get("demo")
    assert images.credentials == ["tok"] and managed._credential is None  # noqa: SLF001
    assert supervisor.desired_generation == "g1"
    # pushing the same set again with a credential does not bring it back: nothing is pulled any more
    supervisor.apply([ENTRY], {}, {"demo": "tok"}, "g2")
    assert managed._credential is None and supervisor.get("demo") is managed  # noqa: SLF001


def test_a_pull_that_fails_is_retried_with_the_credential_the_host_pushed_last(world):
    images = Images(error=RegistryError("registry.test: connection refused"))
    supervisor = world(images)
    supervisor.apply([ENTRY], {}, {"demo": "old"})
    assert wait_for(lambda: images.calls >= 2)

    supervisor.apply([ENTRY], {}, {"demo": "new"})
    images.error = None

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    assert images.credentials[-1] == "new" and "old" in images.credentials


def test_a_refused_image_is_tried_again_when_the_host_pushes_the_set_again(world):
    images = Images(error=ImageError("the registry refuses the credential to musizerr/demo (401)"))
    supervisor = world(images)
    supervisor.apply([ENTRY], {}, {"demo": "wrong"})
    assert wait_for(lambda: state_of(supervisor) == sv.REFUSED)

    images.error = None
    supervisor.apply([ENTRY], {}, {"demo": "right"})

    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    assert images.credentials == ["wrong", "right"]


def test_the_credential_is_in_nothing_the_gateway_reports(world):
    supervisor = world(Images(error=ImageError("the registry refuses the credential to musizerr/demo (401)")))
    supervisor.apply([ENTRY], {}, {"demo": "ghp_canary"})
    assert wait_for(lambda: state_of(supervisor) == sv.REFUSED)
    status = supervisor.get("demo").status

    assert "ghp_canary" not in repr(status) and "ghp_canary" not in status.reason


# ------------------------------------------------------------------
# Folders of the host (filesystem_from_settings)
# ------------------------------------------------------------------


@pytest.fixture
def storage(world, tmp_path):
    """A provider that declares a read-write folder ``root_dir`` and a read-only ``cache_dir``, running
    without any folder until the host grants some."""
    lib = tmp_path / "lib"
    for name in ("music", "music2", "cache"):
        (lib / name).mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    declared = DeclaredManifest(
        "demo", ("storage",), PROVIDER_API_VERSION,
        Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"), FilesystemAccess("cache_dir", "ro"))),
    )
    supervisor = world(Images(declared=declared, rootfs=rootfs))
    supervisor.apply([ENTRY], {})
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)
    supervisor.lib, supervisor.rootfs, supervisor.outside = lib, rootfs, tmp_path / "outside"
    supervisor.managed = supervisor.get("demo")
    supervisor.grant = lambda **paths: supervisor.managed.grant([], 120, None, {k: str(v) for k, v in paths.items()}, wait=10)
    return supervisor


def test_a_provider_that_names_folders_runs_without_them_until_the_host_grants_some(world, storage):
    assert world.built[0].binds == (), "nothing of the host is mounted before a grant: its calls fail cleanly"
    assert storage.managed.status.binds == ()


def test_a_granted_folder_is_bound_at_the_same_path_with_the_mode_of_the_manifest_and_the_provider_restarts(world, storage):
    restarts = storage.managed.status.restarts

    report = storage.grant(root_dir=storage.lib / "music", cache_dir=storage.lib / "cache")

    assert report.restarted and not report.refused
    assert state_of(storage) == sv.RUNNING and len(world.built) == 2
    assert world.built[1].binds == (
        Bind(source=str(storage.lib / "cache"), dest=str(storage.lib / "cache"), mode="ro"),
        Bind(source=str(storage.lib / "music"), dest=str(storage.lib / "music"), mode="rw"),
    )
    assert storage.managed.status.restarts == restarts, "a change of mounts is not a crash"
    assert (storage.rootfs / str(storage.lib / "music").lstrip("/")).is_dir(), "the image has the mount point"


def test_the_same_grant_again_changes_nothing(world, storage):
    storage.grant(root_dir=storage.lib / "music")
    started = len(world.built)
    generation = storage.generation

    report = storage.grant(root_dir=storage.lib / "music")

    assert not report.restarted and len(world.built) == started and storage.generation == generation


def test_a_changed_setting_restarts_the_provider_with_the_new_folder_and_an_empty_one_takes_it_away(world, storage):
    storage.grant(root_dir=storage.lib / "music")

    storage.grant(root_dir=storage.lib / "music2")
    assert [b.dest for b in world.built[-1].binds] == [str(storage.lib / "music2")]

    storage.managed.grant([], 120, None, {})
    assert world.built[-1].binds == () and state_of(storage) == sv.RUNNING and len(world.built) == 4


def test_a_folder_outside_the_roots_is_refused_and_nothing_is_restarted(world, storage):
    report = storage.grant(root_dir=storage.outside, cache_dir="/etc")

    assert not report.restarted and len(world.built) == 1 and report.binds == ()
    assert set(report.refused) == {"root_dir", "cache_dir"}
    assert storage.managed.status.path_refusals == report.refused


def test_a_link_inside_the_root_that_leads_out_is_refused(world, storage):
    (storage.lib / "escape").symlink_to(storage.outside)

    report = storage.grant(root_dir=storage.lib / "escape")

    assert "leads out" in report.refused["root_dir"] and not report.restarted and world.built[-1].binds == ()


def test_a_link_that_is_moved_after_the_grant_is_caught_when_the_provider_starts_again(world, storage):
    storage.grant(root_dir=storage.lib / "music")
    assert [b.dest for b in world.built[-1].binds] == [str(storage.lib / "music")]
    (storage.lib / "music").rmdir()
    (storage.lib / "music").symlink_to(storage.outside)
    pid = int(Path(str(storage.managed.socket_path) + ".pid").read_text())

    os.kill(pid, signal.SIGKILL)

    assert wait_for(lambda: len(world.built) == 3 and state_of(storage) == sv.RUNNING)
    assert world.built[-1].binds == () and "leads out" in storage.managed.status.path_refusals["root_dir"]


def test_a_setting_the_manifest_does_not_declare_is_not_a_folder_to_grant(world, storage):
    report = storage.managed.grant([], 120, None, {"other": str(storage.lib / "music")})

    assert "not a setting this provider declared" in report.refused["other"] and not report.restarted


def test_two_settings_cannot_claim_the_same_folder(world, storage):
    report = storage.grant(root_dir=storage.lib / "music", cache_dir=storage.lib / "music")

    assert report.refused == {"root_dir": "already granted by the setting 'cache_dir'"}
    assert len(world.built[-1].binds) == 1


def test_the_workspace_directory_is_covered_when_the_folder_that_holds_it_is_granted(world, storage):
    storage.grant(root_dir=storage.lib)

    (bind,) = world.built[-1].binds
    assert bind.masks == (str(world.config.workspace_dir),)


def test_a_provider_without_folders_is_never_restarted_by_a_grant(world):
    supervisor = world()
    supervisor.apply([ENTRY], {})
    assert wait_for(lambda: state_of(supervisor) == sv.RUNNING)

    report = supervisor.get("demo").grant(["a.lan:1"], 60, None, {"root_dir": "/downloads/x"})

    assert not report.restarted and len(world.built) == 1 and "root_dir" in report.refused
