"""
Derives the gateway container's seccomp profile from Docker's default one.

    python derive.py moby-default.json > musizeer-gateway.json

The profile is Docker's default profile (``moby-default.json``, copied verbatim
from the vendored ``github.com/moby/profiles/seccomp`` of moby tag
``docker-v29.4.0``) plus ONE appended entry that allows, unconditionally, the
syscalls bubblewrap needs to build a sandbox as an unprivileged user and that
the default profile only grants to a container with ``CAP_SYS_ADMIN``:

- ``unshare`` and ``clone``: it creates the user, mount, PID, IPC, UTS, cgroup
  and network namespaces (the default profile lets ``clone`` through only
  without namespace flags);
- ``mount`` and ``umount2``: it builds the sandbox's mount tree;
- ``pivot_root``: it switches to it (the default profile does not know this
  syscall at all).

Nothing else changes: ``clone3`` keeps answering ``ENOSYS`` (libc falls back
to ``clone``), ``setns``, ``bpf``, ``perf_event_open``, ``fsopen``/``fsmount``/
``move_mount``/``open_tree``/``mount_setattr``, ``keyctl``, ``ptrace``'s
cross-process variants and the rest stay as Docker has them. The container is
not given a capability.

The processes the gateway starts INHERIT this profile (a seccomp filter cannot
be removed), so the sandbox adds its own filter on top of it that takes these
syscalls away again from the provider (``musizeer_gateway/sandbox_filter.py``).
"""
from __future__ import annotations

import json
import sys

ADDED_SYSCALLS = ["clone", "mount", "pivot_root", "umount2", "unshare"]

ADDED_ENTRY = {
    "names": ADDED_SYSCALLS,
    "action": "SCMP_ACT_ALLOW",
    "comment": "bubblewrap builds the provider sandbox (namespaces, mounts, pivot_root); "
               "the sandbox's own filter takes these away from the provider",
}


def derive(default_profile: dict) -> dict:
    profile = json.loads(json.dumps(default_profile))
    profile["syscalls"].append(ADDED_ENTRY)
    return profile


def render(profile: dict) -> str:
    return json.dumps(profile, indent="\t") + "\n"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as source:
        sys.stdout.write(render(derive(json.load(source))))
