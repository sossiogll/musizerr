"""The container's seccomp profile is Docker's default plus exactly what bubblewrap needs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

SECCOMP = Path(__file__).resolve().parent.parent / "seccomp"
MOBY_SHA256 = "01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28"


def derive_module():
    spec = importlib.util.spec_from_file_location("derive", SECCOMP / "derive.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_copy_of_dockers_default_profile_is_the_one_it_says_it_is():
    assert hashlib.sha256((SECCOMP / "moby-default.json").read_bytes()).hexdigest() == MOBY_SHA256


def test_the_shipped_profile_is_what_the_derivation_makes_of_it():
    derive = derive_module()
    default = json.loads((SECCOMP / "moby-default.json").read_text())

    assert (SECCOMP / "musizerr-gateway.json").read_text() == derive.render(derive.derive(default))


def test_it_differs_from_the_default_by_one_appended_entry_of_five_syscalls():
    default = json.loads((SECCOMP / "moby-default.json").read_text())
    shipped = json.loads((SECCOMP / "musizerr-gateway.json").read_text())

    assert {k: v for k, v in shipped.items() if k != "syscalls"} == {k: v for k, v in default.items() if k != "syscalls"}
    assert shipped["syscalls"][:-1] == default["syscalls"]
    added = shipped["syscalls"][-1]
    assert added["action"] == "SCMP_ACT_ALLOW" and sorted(added["names"]) == ["clone", "mount", "pivot_root", "umount2", "unshare"]
    assert not {"includes", "excludes", "args"} & set(added), "unconditional: the container has no capability to condition on"


def test_nothing_that_widens_the_container_beyond_that_is_in_it():
    shipped = json.loads((SECCOMP / "musizerr-gateway.json").read_text())
    unconditional_allowed = {
        name for entry in shipped["syscalls"] if entry["action"] == "SCMP_ACT_ALLOW" and not entry.get("includes") and not entry.get("excludes")
        and not entry.get("args") for name in entry["names"]
    }

    assert shipped["defaultAction"] == "SCMP_ACT_ERRNO"
    for name in ("setns", "bpf", "perf_event_open", "fsopen", "fsmount", "move_mount", "open_tree", "mount_setattr", "keyctl",
                 "init_module", "reboot", "swapon", "kexec_load", "userfaultfd", "io_uring_setup"):
        assert name not in unconditional_allowed, name
