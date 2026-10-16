"""The sandbox's command line and its seccomp filter. (A real sandbox is the integration test.)"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from musizerr_gateway import sandbox, sandbox_filter as f
from musizerr_gateway.config import Ceilings
from musizerr_gateway.sandbox import Limits, SandboxSpec, SandboxUnavailable, build_command, limits_for
from musizerr_sdk.permissions import Permissions, ResourceLimits


@pytest.fixture(autouse=True)
def binaries(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "prlimit_path", lambda: "/usr/bin/prlimit")


def spec(**kwargs) -> SandboxSpec:
    defaults = dict(
        rootfs=Path("/state/images/x/rootfs"), socket_dir=Path("/run/musizerr/providers/p"),
        egress_dir=Path("/state/egress/p"), workspace_dir=Path("/downloads/.work/p"),
        command=("python", "-m", "musizerr_sdk.runner", "--socket", "/run/provider/provider.sock"),
        limits=Limits(512, 256, 64), uid=10001, gid=10001, image_env={"PATH": "/opt/bin", "SECRET": "x", "LANG": "C.UTF-8"},
    )
    return SandboxSpec(**{**defaults, **kwargs})


def pairs(command: list[str], flag: str) -> list[tuple[str, ...]]:
    arity = {"--ro-bind": 2, "--bind": 2, "--setenv": 2, "--proc": 1, "--dev": 1, "--tmpfs": 1, "--size": 1, "--seccomp": 1}[flag]
    return [tuple(command[i + 1:i + 1 + arity]) for i, part in enumerate(command) if part == flag]


def test_the_sandbox_has_every_namespace_a_read_only_root_and_nothing_inherited():
    command = build_command(spec(), 7)

    for flag in ("--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert ("/state/images/x/rootfs", "/") in pairs(command, "--ro-bind")
    assert pairs(command, "--proc") == [("/proc",)] and pairs(command, "--dev") == [("/dev",)]
    assert pairs(command, "--tmpfs") == [("/tmp",)] and pairs(command, "--size") == [(str(256 * 1024 * 1024),)]
    assert command[command.index("--uid") + 1] == "10001" and command[command.index("--gid") + 1] == "10001"
    assert pairs(command, "--seccomp") == [("7",)]
    assert command[-5:] == ["python", "-m", "musizerr_sdk.runner", "--socket", "/run/provider/provider.sock"]


def test_only_the_providers_own_socket_workspace_and_egress_socket_are_mounted_in():
    command = build_command(spec(), 3)

    assert pairs(command, "--bind") == [
        ("/run/musizerr/providers/p", "/run/provider"), ("/downloads/.work/p", "/workspace"),
    ]
    assert ("/state/egress/p", "/run/egress") in pairs(command, "--ro-bind"), "the egress socket cannot be replaced from inside"
    assert len(pairs(command, "--ro-bind")) == 2, "nothing else of the host is visible"


def test_the_environment_is_only_what_the_runner_needs():
    env = dict(pairs(build_command(spec(), 3), "--setenv"))

    assert env["PATH"] == "/opt/bin" and env["LANG"] == "C.UTF-8"
    assert env["HOME"] == "/tmp" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "SECRET" not in env


def test_the_rlimits_are_set_by_prlimit_around_bubblewrap():
    command = build_command(spec(limits=Limits(512, 256, 64)), 3)

    assert command[0] == "/usr/bin/prlimit"
    assert f"--as={512 * 1024 * 1024}" in command and "--nofile=256" in command and "--nproc=64" in command and "--core=0" in command
    assert command.index("/usr/bin/bwrap") > command.index("--")


def test_the_limits_of_the_manifest_are_capped_at_the_ceilings_of_the_gateway():
    ceilings = Ceilings(memory_mb=1000, open_files=500, processes=100, default_memory_mb=300, default_open_files=200, default_processes=50)

    assert limits_for(Permissions(), ceilings) == Limits(300, 200, 50)
    assert limits_for(Permissions(limits=ResourceLimits(memory_mb=600)), ceilings).memory_mb == 600
    assert limits_for(Permissions(limits=ResourceLimits(memory_mb=10**6, open_files=10**6, processes=10**6)), ceilings) == Limits(1000, 500, 100)


def test_without_bubblewrap_nothing_can_be_run(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)

    with pytest.raises(SandboxUnavailable, match="not installed"):
        build_command(spec(), 3)
    with pytest.raises(SandboxUnavailable):
        sandbox.probe()


# ------------------------------------------------------------------
# The seccomp filter, run by a small interpreter of classic BPF
# ------------------------------------------------------------------


def run_filter(arch: int, nr: int, arg0: int = 0) -> int:
    program = [struct.unpack("<HBBI", f.compile_program()[i:i + 8]) for i in range(0, len(f.compile_program()), 8)]
    data = {f.OFFSET_NR: nr, f.OFFSET_ARCH: arch, f.OFFSET_ARG0: arg0 & 0xFFFFFFFF}
    accumulator, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        pc += 1
        if code == f.LD_W_ABS:
            accumulator = data[k]
        elif code == f.ALU_AND_K:
            accumulator &= k
        elif code == f.JMP_JEQ_K:
            pc += jt if accumulator == k else jf
        elif code == f.JMP_JGE_K:
            pc += jt if accumulator >= k else jf
        elif code == f.RET_K:
            return k
        else:
            raise AssertionError(f"opcode {code:#x}")


ARCHS = {"x86_64": f.AUDIT_ARCH_X86_64, "aarch64": f.AUDIT_ARCH_AARCH64}
EPERM = f.SECCOMP_RET_ERRNO | f.EPERM
ENOSYS = f.SECCOMP_RET_ERRNO | f.ENOSYS


@pytest.mark.parametrize("arch", ARCHS)
def test_namespaces_and_mounts_are_denied_to_the_provider(arch):
    for name, number in f.DENIED[arch].items():
        assert run_filter(ARCHS[arch], number) == EPERM, name
    assert run_filter(ARCHS[arch], f.CLONE3[arch]) == ENOSYS, "clone3 falls back to clone, whose flags can be read"


@pytest.mark.parametrize("arch", ARCHS)
def test_clone_is_denied_only_with_a_namespace_flag(arch):
    clone = f.CLONE[arch]

    assert run_filter(ARCHS[arch], clone, 0x00000011) == f.SECCOMP_RET_ALLOW, "fork: SIGCHLD"
    assert run_filter(ARCHS[arch], clone, 0x003D0F00) == f.SECCOMP_RET_ALLOW, "a thread: CLONE_VM|FS|FILES|SIGHAND|THREAD|SYSVSEM"
    for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
        assert run_filter(ARCHS[arch], clone, 0x00000011 | flag) == EPERM, hex(flag)


@pytest.mark.parametrize("arch, read, write, openat", [("x86_64", 0, 1, 257), ("aarch64", 63, 64, 56)])
def test_everything_else_is_left_to_the_containers_profile(arch, read, write, openat):
    for number in (read, write, openat):
        assert run_filter(ARCHS[arch], number) == f.SECCOMP_RET_ALLOW


def test_an_unknown_architecture_and_the_x32_range_are_not_let_through():
    assert run_filter(0x40000003, 0) == f.SECCOMP_RET_KILL_PROCESS, "32-bit x86"
    assert run_filter(f.AUDIT_ARCH_X86_64, 0x40000000 | 272) == ENOSYS, "unshare through the x32 numbers"


def test_the_program_is_a_whole_number_of_instructions_with_short_jumps():
    blob = f.compile_program()

    assert len(blob) % 8 == 0 and len(blob) // 8 < 200


# ------------------------------------------------------------------
# Folders of the host granted by settings
# ------------------------------------------------------------------


def test_a_granted_folder_is_bound_at_the_same_path_read_write_or_read_only():
    from musizerr_gateway.filesystem import Bind

    command = build_command(spec(binds=(
        Bind("/real/music", "/downloads/music", "rw"), Bind("/real/cache", "/downloads/cache", "ro"),
    )), 3)

    assert ("/real/music", "/downloads/music") in pairs(command, "--bind")
    assert ("/real/cache", "/downloads/cache") in pairs(command, "--ro-bind")
    assert ("/real/cache", "/downloads/cache") not in pairs(command, "--bind"), "read-only is really read-only"
    assert ("/real/music", "/downloads/music") not in pairs(command, "--ro-bind")


def test_binds_come_after_the_image_and_in_path_order_and_the_workspace_directory_is_covered():
    from musizerr_gateway.filesystem import Bind

    command = build_command(spec(binds=(
        Bind("/real/lib/music", "/lib/music", "rw"),
        Bind("/real/lib", "/lib", "rw", masks=("/lib/.work",)),
    )), 3)

    flags = [part for part in command if part in ("--bind", "--ro-bind", "--tmpfs")]
    assert command.index("/lib") < command.index("/lib/music"), "an outer folder is mounted before the one inside it"
    assert command.index("/lib/.work") > command.index("/lib") and command.index("/lib/.work") < command.index("/lib/music")
    assert pairs(command, "--tmpfs") == [("/tmp",), ("/lib/.work",)]
    assert command.index("/state/images/x/rootfs") < command.index("/real/lib"), "mounted over the image's root"
    assert flags


def test_without_grants_nothing_but_the_providers_own_folders_is_mounted():
    assert len(pairs(build_command(spec(), 3), "--bind")) == 2
