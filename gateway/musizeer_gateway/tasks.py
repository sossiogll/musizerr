"""
Processes and threads: how many a sandbox holds, and what the container allows.

**Why the kernel's ``RLIMIT_NPROC`` is not used for a sandbox.** It is not a limit of a process or of a
sandbox: the kernel checks it against the number of tasks (processes AND threads) of the *real user id* in
the whole machine. The sandboxes run as the gateway's uid, and so does everything else that runs as that uid
on the host, in any container. A gateway started as ``user: "1000:1000"`` shares the uid with the main
account of a typical host and with whatever else runs as it; with hundreds of such tasks already there, a
``--nproc=512`` makes ``pthread_create`` fail with ``EAGAIN`` ("can't start new thread") in a provider that
holds two threads. Even under a uid of its own it would be one cap for the gateway and all its sandboxes
together, so a provider that asks for a small limit would starve the others. It is therefore not set.

What bounds the tasks instead:

- the **container's pids limit** (``pids_limit`` in the compose file, the cgroup ``pids`` controller): a real
  hard cap, per container, that nothing else on the host counts toward. A fork bomb in a sandbox ends there,
  and the host never feels it;
- the **per-sandbox cap**, the manifest's ``limits.processes`` within the gateway's ceiling: the gateway
  counts the tasks of each sandbox (its process tree, :func:`read_table` / :func:`tasks_below`) every few
  seconds and stops a provider that stays above its cap (``supervisor.ManagedProvider``). It is a sampling
  watchdog, not a kernel limit: it catches a provider that leaks threads or processes, the cgroup limit is
  what stops a burst between two samples.

This module holds the pure parts (reading ``/proc`` and the cgroup, recognising a provider's "cannot create a
thread" message, wording the numbers); it knows nothing of providers.
"""
from __future__ import annotations

import os
import resource
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROC = Path("/proc")
# Where a container sees its own pids controller: cgroup v2 (a private cgroup namespace) and v1.
CGROUP_PIDS_DIRS = (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/pids"))


def read_table(proc: Path = PROC) -> dict[int, tuple[int, int]]:
    """``pid -> (parent pid, threads)`` for every process ``proc`` shows. A process that exits while the
    table is read is simply missing from it."""
    table: dict[int, tuple[int, int]] = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            text = (proc / name / "status").read_text()
        except OSError:
            continue
        parent = threads = None
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parent = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
        if parent is not None and threads is not None:
            table[int(name)] = (parent, threads)
    return table


def tasks_below(table: Mapping[int, tuple[int, int]], root: int) -> int:
    """The tasks (threads, which counts each process once at least) of ``root`` and all its descendants:
    what the kernel counts against ``RLIMIT_NPROC``, for one sandbox. 0 when ``root`` is not in the table."""
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    total, seen, pending = 0, set(), [root]
    while pending:
        pid = pending.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        total += table[pid][1]
        pending.extend(children.get(pid, ()))
    return total


@dataclass(frozen=True)
class PidsCgroup:
    """The container's pids controller: how many tasks it holds and its limit (``None``: unlimited)."""

    current: int
    limit: int | None


def read_pids_cgroup(directories: tuple[Path, ...] = CGROUP_PIDS_DIRS) -> PidsCgroup | None:
    """The pids controller of this container, or ``None`` when it cannot be read."""
    for directory in directories:
        try:
            current = int((directory / "pids.current").read_text().strip())
            raw = (directory / "pids.max").read_text().strip()
        except (OSError, ValueError):
            continue
        return PidsCgroup(current, None if raw == "max" else int(raw))
    return None


# What a runtime says when the kernel refused it a thread or a process (EAGAIN): CPython, Go, glibc/pthread and
# a shell. "resource temporarily unavailable" alone is also what a non-blocking read says, so it counts only
# next to a word about creating a task.
_EXHAUSTION = (
    "can't start new thread", "cannot start new thread", "failed to create new os thread", "pthread_create failed",
    "cannot fork", "unable to fork", "fork: retry",
)
_TASK_WORDS = ("fork", "thread", "clone", "spawn")


def is_exhaustion(line: str) -> bool:
    """Whether a line of a provider's output says it could not create a thread or a process."""
    text = line.lower()
    if any(marker in text for marker in _EXHAUSTION):
        return True
    return "resource temporarily unavailable" in text and any(word in text for word in _TASK_WORDS)


def _limit_text(value: int) -> str:
    return "unlimited" if value == resource.RLIM_INFINITY else str(value)


_READ_NOW: Any = object()


def pressure_summary(
    cgroup: PidsCgroup | None = _READ_NOW, nproc: tuple[int, int] | None = None, uid: int | None = None,
    sandboxes: Mapping[str, tuple[int, int, int]] | None = None,
) -> str:
    """The numbers behind "the kernel would not create a task", in one line: this container's pids (and its
    limit), the ``RLIMIT_NPROC`` the gateway runs under (counted for the whole uid on the host), and the tasks
    of each sandbox against its cap, with the address space its threads' stacks are reserved in
    (``name -> (tasks, limits.processes, limits.memory_mb)``). The arguments are for tests; by default
    everything is read now."""
    if cgroup is _READ_NOW:
        cgroup = read_pids_cgroup()
    if nproc is None:
        nproc = resource.getrlimit(resource.RLIMIT_NPROC)
    uid = os.getuid() if uid is None else uid
    parts: list[str] = []
    if isinstance(cgroup, PidsCgroup):
        if cgroup.limit is None:
            parts.append(f"container pids: {cgroup.current}, no limit set (pids_limit)")
        else:
            parts.append(f"container pids: {cgroup.current} of {cgroup.limit} (pids_limit)")
    else:
        parts.append("container pids: unknown")
    parts.append(
        f"RLIMIT_NPROC of the gateway: {_limit_text(nproc[0])}, counted over every process of uid {uid} on the whole host"
    )
    for name, (held, cap, memory_mb) in sorted((sandboxes or {}).items()):
        parts.append(
            f"sandbox {name}: {held} of {cap} tasks (limits.processes), address space limited to {memory_mb} MB (limits.memory_mb; "
            f"every thread reserves its stack in it)"
        )
    return "; ".join(parts)


TASK_LIMIT_HINT = (
    "EAGAIN: the kernel would not create a process or a thread. The container's pids limit was reached "
    "(pids_limit of the gateway service in the compose file), or an RLIMIT_NPROC was: the kernel counts it for every "
    "process of the gateway's uid on the whole host, other containers included (docker run --ulimit nproc, dockerd's "
    "default-ulimits, a systemd LimitNPROC; the gateway sets none of its own), or the sandbox ran out of address "
    "space (limits.memory_mb), where every thread reserves its stack"
)
