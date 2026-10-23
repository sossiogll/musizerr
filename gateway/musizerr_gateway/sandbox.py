"""
The bubblewrap command line of a provider's sandbox, and the check that a sandbox
can be made at all.

What the provider gets:

- the image's root filesystem, READ-ONLY, as ``/``; a private ``tmpfs`` ``/tmp``; a fresh
  ``/proc`` and a minimal ``/dev``;
- every namespace unshared: user (mandatory, never "try"), mount, PID, IPC, UTS, cgroup
  and NETWORK. With no network at all, the only way out is the egress proxy's Unix
  socket, bound read-only at ``/run/egress``;
- the folders its settings name, when the gateway grants them (``filesystem.py``), at the same absolute path,
  read-only or read-write as its manifest says;
- ``/run/provider`` (the directory of its own socket) and ``/workspace`` (its own subtree
  of the host's workspace directory, nothing else of it) read-write;
- an empty environment (``--clearenv``) plus a short list of variables, one non-root uid
  and gid, no capability, ``no_new_privs``, a new session, and death with the gateway;
- the seccomp filter of :mod:`sandbox_filter` and, with bubblewrap's own
  ``--disable-userns``, no further user namespaces;
- the rlimits of its manifest within the ceilings of the gateway, set by ``prlimit``: address space and open
  files (limits of a process, so they mean the same everywhere). NOT the number of processes: the kernel's
  ``RLIMIT_NPROC`` is counted over every process of the uid on the whole host, so it cannot be a limit of one
  sandbox (``tasks.py`` says what bounds the tasks instead: the container's pids limit and the gateway's own
  count of each sandbox, ``Limits.processes``).

**Fail closed.** If bubblewrap cannot make the sandbox (not installed, no user
namespaces, the container's seccomp profile or masked ``/proc`` in the way) the
provider does NOT run: :func:`probe` says why and nothing runs unconfined.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

from musizerr_sdk.permissions import Permissions

from .config import Ceilings
from .filesystem import Bind
from .sandbox_filter import compile_program
from .tasks import TASK_LIMIT_HINT, pressure_summary

SANDBOX_WORKSPACE = "/workspace"
SANDBOX_SOCKET_DIR = "/run/provider"
SANDBOX_EGRESS_DIR = "/run/egress"
SOCKET_NAME = "provider.sock"
EGRESS_SOCKET_NAME = "egress.sock"

# The variables of the image's environment a provider keeps; the rest is dropped.
IMAGE_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH")
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
PROBE_TIMEOUT_SECONDS = 20


class SandboxUnavailable(RuntimeError):
    """A sandbox cannot be made here; the message says why."""


# What bubblewrap says when it cannot build a sandbox, read as a diagnosis: (words that must ALL appear in
# the lowercased error text, what the administrator should do). The first row that matches wins, so the
# specific ones come first. The text is bubblewrap's own (``bwrap.c``): EACCES on the namespace call reads
# "Creating new namespace failed: Permission denied" and is an LSM (AppArmor) denial; EPERM reads "No
# permissions to create a new namespace" and is seccomp or a sysctl. The hints are about the container's
# configuration, never about loosening the sandbox: ``privileged`` is not one of them (docs/gateway.md).
SANDBOX_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # EAGAIN on clone/unshare/fork: no task could be created (not a permission problem; the numbers are added by explain_failure).
    (("resource temporarily unavailable",), TASK_LIMIT_HINT),
    (("creating new namespace failed", "permission denied"),
     "EACCES: the container is confined by AppArmor (Docker's docker-default profile); add apparmor=unconfined to the "
     "gateway's security_opt (never privileged)"),
    (("uid map", "permission denied"),
     "EACCES: an AppArmor policy denies the user namespace its uid map (on Ubuntu, kernel.apparmor_restrict_unprivileged_userns); "
     "add apparmor=unconfined to the gateway's security_opt (never privileged)"),
    (("no permissions to creat", "namespace"),
     "EPERM: the seccomp profile of the container is not gateway/seccomp/musizerr-gateway.json (security_opt seccomp=...), or "
     "the kernel forbids unprivileged user namespaces (sysctl kernel.unprivileged_userns_clone=1 on Debian, user.max_user_namespaces > 0)"),
    (("kernel does not support user namespaces",),
     "EINVAL: the kernel has no user namespaces, or user.max_user_namespaces is 0 (sysctl user.max_user_namespaces=15000)"),
    (("can't mount proc",),
     "the container's /proc is masked: add systempaths=unconfined to the gateway's security_opt"),
    (("pivot_root",),
     "the seccomp profile of the container is not gateway/seccomp/musizerr-gateway.json (security_opt seccomp=...)"),
    (("can't mount", "operation not permitted"),
     "the seccomp profile of the container is not gateway/seccomp/musizerr-gateway.json (security_opt seccomp=...)"),
    (("operation not permitted",),
     "EPERM: the seccomp profile of the container is not gateway/seccomp/musizerr-gateway.json (security_opt seccomp=...), or "
     "the kernel forbids unprivileged user namespaces (kernel.unprivileged_userns_clone, user.max_user_namespaces)"),
    (("permission denied",),
     "EACCES: probably AppArmor; add apparmor=unconfined to the gateway's security_opt (never privileged)"),
)


def explain_failure(error_text: str) -> str:
    """The first line of what bubblewrap said, followed by what to do about it when the text is a
    known one (``SANDBOX_HINTS``). Always one line: it becomes ``sandbox unavailable: <reason>``."""
    lines = [line.strip() for line in error_text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    lowered = error_text.lower()
    for words, hint in SANDBOX_HINTS:
        if all(word in lowered for word in words):
            if hint is TASK_LIMIT_HINT:
                hint = f"{hint} [{pressure_summary()}]"
            return f"{lines[0]} (hint: {hint}; docs/gateway.md, Troubleshooting)"
    return lines[0]


@dataclass(frozen=True)
class Limits:
    memory_mb: int  # address space of each process (RLIMIT_AS)
    open_files: int  # open files of each process (RLIMIT_NOFILE)
    processes: int  # tasks (processes and threads) of the whole sandbox: counted by the gateway, not by the kernel (tasks.py)


def limits_for(permissions: Permissions, ceilings: Ceilings) -> Limits:
    """The manifest's limits, capped at the ceilings; the defaults for what it leaves out."""
    asked = permissions.limits
    return Limits(
        memory_mb=min(asked.memory_mb or ceilings.default_memory_mb, ceilings.memory_mb),
        open_files=min(asked.open_files or ceilings.default_open_files, ceilings.open_files),
        processes=min(asked.processes or ceilings.default_processes, ceilings.processes),
    )


@dataclass(frozen=True)
class SandboxSpec:
    rootfs: Path
    socket_dir: Path
    egress_dir: Path
    workspace_dir: Path
    command: tuple[str, ...]
    limits: Limits
    uid: int
    gid: int
    tmp_mb: int = 256
    image_env: Mapping[str, str] = field(default_factory=dict)
    # Folders of the host granted by the settings the provider names (``filesystem.py``), bound at the
    # same absolute path; the image's root filesystem must already hold the mount points.
    binds: tuple[Bind, ...] = ()


def sandbox_environment(image_env: Mapping[str, str]) -> dict[str, str]:
    env = {key: value for key, value in image_env.items() if key in IMAGE_ENV_KEEP}
    env.setdefault("PATH", DEFAULT_PATH)
    env.update({"HOME": "/tmp", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def bwrap_path() -> str:
    path = shutil.which("bwrap")
    if path is None:
        raise SandboxUnavailable("bubblewrap (bwrap) is not installed in the gateway")
    return path


def prlimit_path() -> str:
    path = shutil.which("prlimit")
    if path is None:
        raise SandboxUnavailable("prlimit (util-linux) is not installed: the resource limits cannot be set")
    return path


def _common_arguments(uid: int, gid: int) -> list[str]:
    return [
        "--unshare-all", "--unshare-user", "--disable-userns",
        "--die-with-parent", "--new-session", "--clearenv",
        "--uid", str(uid), "--gid", str(gid), "--cap-drop", "ALL",
    ]


def build_command(spec: SandboxSpec, seccomp_fd: int) -> list[str]:
    """The command line that runs ``spec.command`` in the sandbox."""
    limits = spec.limits
    command = [
        prlimit_path(),
        f"--as={limits.memory_mb * 1024 * 1024}", f"--nofile={limits.open_files}", "--core=0",
        "--",
        bwrap_path(), *_common_arguments(spec.uid, spec.gid),
        "--ro-bind", str(spec.rootfs), "/",
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(spec.tmp_mb * 1024 * 1024), "--tmpfs", "/tmp",
        "--bind", str(spec.socket_dir), SANDBOX_SOCKET_DIR,
        "--ro-bind", str(spec.egress_dir), SANDBOX_EGRESS_DIR,
        "--bind", str(spec.workspace_dir), SANDBOX_WORKSPACE,
        *_granted_binds(spec.binds),
        "--seccomp", str(seccomp_fd),
        "--chdir", "/",
    ]
    for key, value in sandbox_environment(spec.image_env).items():
        command += ["--setenv", key, value]
    return [*command, "--", *spec.command]


@contextmanager
def seccomp_file() -> Iterator[int]:
    """A file descriptor holding the sandbox's seccomp program, for ``--seccomp``."""
    with tempfile.TemporaryFile() as handle:
        handle.write(compile_program())
        handle.flush()
        handle.seek(0)
        os.set_inheritable(handle.fileno(), True)
        yield handle.fileno()


def probe(uid: int = 10001, gid: int = 10001) -> None:
    """Make a throwaway sandbox exactly as a provider's is made (same namespaces,
    mounts, seccomp filter), run the gateway's own interpreter in it and look at the
    result. Raises :class:`SandboxUnavailable` with the reason when it fails."""
    bwrap = bwrap_path()
    prlimit_path()
    interpreter = os.path.realpath(sys.executable)
    with seccomp_file() as fd:
        command = [
            bwrap, *_common_arguments(uid, gid),
            "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--seccomp", str(fd), "--",
            interpreter, "-c", "import os; assert os.getuid() == %d" % uid,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, pass_fds=(fd,), check=False, env={},
            )
        except subprocess.TimeoutExpired:
            raise SandboxUnavailable("the probe sandbox did not start within %d s" % PROBE_TIMEOUT_SECONDS) from None
        except OSError as exc:
            raise SandboxUnavailable(f"bubblewrap cannot be run: {exc}") from exc
    if result.returncode != 0:
        raise SandboxUnavailable(explain_failure(result.stderr) or f"exit status {result.returncode}")


def _granted_binds(binds: tuple[Bind, ...]) -> list[str]:
    arguments: list[str] = []
    for bind in sorted(binds, key=lambda item: item.dest):
        arguments += ["--bind" if bind.mode == "rw" else "--ro-bind", bind.source, bind.dest]
        for mask in bind.masks:
            arguments += ["--tmpfs", mask]
    return arguments
