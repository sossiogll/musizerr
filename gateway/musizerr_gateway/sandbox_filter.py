"""
The seccomp filter the sandboxed provider runs under, on top of the container's.

Why it exists. The gateway container needs ``unshare``, ``clone`` with namespace
flags, ``mount``, ``umount2`` and ``pivot_root`` for bubblewrap to build a sandbox
as an unprivileged user (``seccomp/musizerr-gateway.json``). A seccomp filter is
inherited and cannot be removed, so without more the PROVIDER would hold the very
same permissions inside its sandbox: it could create a nested user namespace and
mount in it. This filter takes them away again. Bubblewrap loads it (``--seccomp
FD``) just before it executes the provider, after its own setup is done.

It is a denylist over the container's allowlist (Docker's default profile), so it
only names what the container's profile lets through that a provider must not do:

- namespaces and mounts: ``unshare``, ``setns``, ``clone`` with a namespace flag,
  ``clone3`` (answered ``ENOSYS``, so libc falls back to ``clone`` and the flags can be
  read), ``mount``, ``umount2``, ``pivot_root``, ``chroot`` and the new mount API
  (``open_tree``, ``move_mount``, ``fsopen``, ``fsconfig``, ``fsmount``, ``fspick``,
  ``mount_setattr``);
- the x32 system call range of x86-64, and any architecture it does not know.

A classic BPF program is written by hand (no third-party library, no libseccomp), for
x86-64 and AArch64.
"""
from __future__ import annotations

import struct

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
ENOSYS = 38

# clone flags that create a namespace (CLONE_NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET).
CLONE_NAMESPACE_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
X32_SYSCALL_BIT = 0x40000000

# BPF opcodes (linux/filter.h)
LD_W_ABS = 0x20
ALU_AND_K = 0x54
JMP_JEQ_K = 0x15
JMP_JGE_K = 0x35
JMP_JA = 0x05
RET_K = 0x06

# seccomp_data: nr at 0, arch at 4, args from 16 (little endian: the low word first).
OFFSET_NR = 0
OFFSET_ARCH = 4
OFFSET_ARG0 = 16

# Syscalls answered EPERM, by name and number, per architecture.
DENIED: dict[str, dict[str, int]] = {
    "x86_64": {
        "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "unshare": 272, "setns": 308,
        "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "mount_setattr": 442,
    },
    "aarch64": {
        "mount": 40, "umount2": 39, "pivot_root": 41, "chroot": 51, "unshare": 97, "setns": 268,
        "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "mount_setattr": 442,
    },
}
CLONE = {"x86_64": 56, "aarch64": 220}
CLONE3 = {"x86_64": 435, "aarch64": 435}
ARCH = {"x86_64": AUDIT_ARCH_X86_64, "aarch64": AUDIT_ARCH_AARCH64}

Instruction = tuple[int, int, int, int]  # code, jt, jf, k


def _ret(value: int) -> Instruction:
    return (RET_K, 0, 0, value)


def _arch_section(arch: str) -> list[Instruction]:
    """What to do with a system call of ``arch``: the program continues here with the
    architecture already checked."""
    code: list[Instruction] = [(LD_W_ABS, 0, 0, OFFSET_NR)]
    if arch == "x86_64":
        # The x32 ABI shares the architecture number and sets this bit in the call number.
        code += [(JMP_JGE_K, 0, 1, X32_SYSCALL_BIT), _ret(SECCOMP_RET_ERRNO | ENOSYS)]
    for number in DENIED[arch].values():
        code += [(JMP_JEQ_K, 0, 1, number), _ret(SECCOMP_RET_ERRNO | EPERM)]
    code += [(JMP_JEQ_K, 0, 1, CLONE3[arch]), _ret(SECCOMP_RET_ERRNO | ENOSYS)]
    code += [
        (JMP_JEQ_K, 0, 4, CLONE[arch]),
        (LD_W_ABS, 0, 0, OFFSET_ARG0),
        (ALU_AND_K, 0, 0, CLONE_NAMESPACE_FLAGS),
        (JMP_JEQ_K, 1, 0, 0),
        _ret(SECCOMP_RET_ERRNO | EPERM),
    ]
    code.append(_ret(SECCOMP_RET_ALLOW))
    return code


def program() -> list[Instruction]:
    x86 = _arch_section("x86_64")
    arm = _arch_section("aarch64")
    head: list[Instruction] = [
        (LD_W_ABS, 0, 0, OFFSET_ARCH),
        (JMP_JEQ_K, 2, 0, AUDIT_ARCH_X86_64),            # -> the x86-64 section, after the kill below
        (JMP_JEQ_K, 1 + len(x86), 0, AUDIT_ARCH_AARCH64),  # -> the AArch64 section
        _ret(SECCOMP_RET_KILL_PROCESS),                  # any other architecture
    ]
    return head + x86 + arm


def compile_program() -> bytes:
    """The filter as the array of ``struct sock_filter`` bubblewrap's ``--seccomp`` reads."""
    instructions = program()
    for code, jt, jf, _k in instructions:
        if not (0 <= jt <= 255 and 0 <= jf <= 255):
            raise ValueError("a jump of the seccomp program does not fit 8 bits")
    return b"".join(struct.pack("<HBBI", code, jt, jf, k) for code, jt, jf, k in instructions)
