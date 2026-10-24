"""
The root folders the host declares, and which of them the gateway accepts (ADR-0008 §9).

There is no list of folders in the gateway's environment. The host (the administrator, in the interface) declares
**root folders** and the **working folder** and pushes them with the desired set of providers (``{"op": "install"}``,
``control.py``); this module decides what the gateway does with them. The bound on what an administrator can declare is
what Docker mounted into this container: a root is accepted only if

- it is an absolute, normalized folder other than ``/``, and collides with nothing every sandbox mounts itself
  (``filesystem.check_root``);
- it does not overlap the gateway's own folders (its state and run directories);
- it exists, is a directory and is writable;
- it lies on a MOUNTED VOLUME: the longest entry of the gateway's own ``/proc/self/mountinfo`` that contains its resolved
  path is neither the root of the container's file system nor a pseudo or memory file system (``tmpfs``, ``proc``...).
  A root cannot be ``/etc`` or any other part of the image;
- no other root has the same path (a root may lie inside another; the same path twice is refused).

The working folder is accepted only if it lies inside an accepted root (a link that leaves it is refused).

``inspect`` and ``browse`` are the read-only operations the interface uses to pick and check a folder. ``browse`` lists the
subdirectories of a folder inside a mounted volume (never files, a link that leads out of the volume is not listed) and,
with no path, the mounted volumes themselves.

Everything here reads the file system and a list of mounts it is given, so it can be tested with a fake one.
"""
from __future__ import annotations

import os
import posixpath
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .filesystem import MAX_PATH, PathRefused, check_root, inside

MOUNTINFO = "/proc/self/mountinfo"
MAX_ROOTS = 64
MAX_BROWSE_ENTRIES = 2000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# File systems that are not a volume somebody mounted into the container: kernel interfaces and memory.
PSEUDO_FILESYSTEMS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2", "mqueue", "securityfs", "debugfs",
    "tracefs", "configfs", "fusectl", "pstore", "bpf", "autofs", "binfmt_misc", "hugetlbfs", "nsfs", "selinuxfs",
    "efivarfs",
})


class FoldersError(ValueError):
    """The folders the host pushed are not a usable message (the install is refused as a whole)."""


@dataclass(frozen=True)
class Mount:
    """One line of ``/proc/self/mountinfo``."""

    mount_id: int
    parent_id: int
    device: str  # "major:minor"
    root: str  # the path inside the file system that is this mount's root
    mount_point: str
    fs_type: str
    source: str = ""


def _unescape(text: str) -> str:
    """The octal escapes (``\\040`` is a space) mountinfo writes into a path."""
    return re.sub(r"\\([0-7]{3})", lambda found: chr(int(found.group(1), 8)), text)


def parse_mountinfo(text: str) -> tuple[Mount, ...]:
    """The mounts ``text`` (the content of ``/proc/self/mountinfo``) lists, in file order. A line that cannot be read is skipped."""
    mounts: list[Mount] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mounts.append(Mount(
                mount_id=int(fields[0]), parent_id=int(fields[1]), device=fields[2], root=_unescape(fields[3]),
                mount_point=_unescape(fields[4]), fs_type=fields[separator + 1],
                source=_unescape(fields[separator + 2]) if len(fields) > separator + 2 else "",
            ))
        except (ValueError, IndexError):
            continue
    return tuple(mounts)


def read_mountinfo(path: str = MOUNTINFO) -> tuple[Mount, ...]:
    """The container's own mounts. An unreadable table is NO mount: nothing then is a mounted volume, so no root is accepted."""
    try:
        return parse_mountinfo(Path(path).read_text(errors="replace"))
    except OSError:
        return ()


def mount_of(path: str, mounts: Sequence[Mount]) -> Mount | None:
    """The mount that holds ``path``: the longest mount point that contains it; of several at the same point (a
    mount laid over another) the last one listed, the visible one."""
    best: Mount | None = None
    for mount in mounts:
        if inside(path, mount.mount_point) and (best is None or len(mount.mount_point) >= len(best.mount_point)):
            best = mount
    return best


def volume_problem(mount: Mount | None) -> str | None:
    """Why the mount ``mount`` is not a volume mounted into the container, or ``None`` when it is."""
    if mount is None:
        return "it is on no mount the container lists"
    if mount.mount_point == "/":
        return "it is part of the container's own file system, not a mounted volume (mount a volume or a host folder there)"
    if mount.fs_type in PSEUDO_FILESYSTEMS:
        return f"it is on a {mount.fs_type} file system, not a mounted volume"
    return None


@dataclass(frozen=True)
class DeclaredRoot:
    id: str
    path: str


@dataclass(frozen=True)
class DeclaredFolders:
    """What the host pushed: its root folders and its working folder (``None``: not set)."""

    roots: tuple[DeclaredRoot, ...] = ()
    workspace: str | None = None


def parse_folders(roots: object, workspace: object) -> DeclaredFolders:
    """The ``roots`` and ``workspace`` of an install message. :class:`FoldersError` when they are not the right shape;
    whether a root can be USED is :func:`evaluate`'s to say."""
    if roots is None:
        roots = []
    if not isinstance(roots, list):
        raise FoldersError("'roots' is a list of {id, path}")
    if len(roots) > MAX_ROOTS:
        raise FoldersError(f"more than {MAX_ROOTS} root folders")
    declared: list[DeclaredRoot] = []
    for item in roots:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
            raise FoldersError("a root is {id, path}, both text")
        if not _ID.match(item["id"]):
            raise FoldersError(f"'{item['id']}' is not a root id")
        if any(root.id == item["id"] for root in declared):
            raise FoldersError(f"the root id '{item['id']}' is listed twice")
        declared.append(DeclaredRoot(item["id"], item["path"]))
    if workspace is not None and (not isinstance(workspace, str) or not workspace):
        raise FoldersError("'workspace' is a folder or null")
    return DeclaredFolders(tuple(declared), workspace)


@dataclass(frozen=True)
class RootReport:
    id: str
    path: str
    state: str  # "accepted" | "refused"
    reason: str = ""
    device: str | None = None
    mount_id: int | None = None
    mount_point: str | None = None
    fs_type: str | None = None
    writable: bool | None = None
    free_bytes: int | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "path": self.path, "state": self.state, "reason": self.reason, "device": self.device,
            "mount_id": self.mount_id, "mount_point": self.mount_point, "fs_type": self.fs_type,
            "writable": self.writable, "free_bytes": self.free_bytes,
        }


@dataclass(frozen=True)
class FolderState:
    """What the gateway makes of the declared folders RIGHT NOW (:func:`evaluate`)."""

    declared: DeclaredFolders
    reports: tuple[RootReport, ...] = ()
    workspace: str | None = None  # the working folder, when it is accepted
    workspace_state: str = "unset"  # "unset" | "accepted" | "refused"
    workspace_reason: str = ""

    @property
    def accepted_roots(self) -> tuple[str, ...]:
        return tuple(report.path for report in self.reports if report.state == "accepted")

    def workspace_view(self) -> dict:
        return {"path": self.declared.workspace, "state": self.workspace_state, "reason": self.workspace_reason}


def free_bytes(path: str) -> int | None:
    try:
        status = os.statvfs(path)
    except OSError:
        return None
    return status.f_bavail * status.f_frsize


def overlaps_own(path: str, own: Iterable[str]) -> str | None:
    """The gateway folder ``path`` is, holds or lies in, if any: a root folder may be none of these."""
    real = os.path.realpath(path)
    for folder in own:
        folder_real = os.path.realpath(folder)
        if inside(real, folder_real) or inside(folder_real, real) or inside(path, str(folder)) or inside(str(folder), path):
            return str(folder)
    return None


def within_own(path: str, own: Iterable[str]) -> str | None:
    """The gateway folder ``path`` is or lies in, if any (a folder that merely HOLDS one can still be browsed, to pick
    something else inside it)."""
    real = os.path.realpath(path)
    for folder in own:
        if inside(real, os.path.realpath(folder)) or inside(path, str(folder)):
            return str(folder)
    return None


def judge_root(root: DeclaredRoot, mounts: Sequence[Mount], own: Sequence[str], taken: set[str]) -> RootReport:
    """The report on one declared root; ``taken`` holds the paths of the roots already seen (the same path twice is refused)."""
    path = root.path
    try:
        check_root(path)
        if len(path) > MAX_PATH:
            raise PathRefused("not a usable path")
    except PathRefused as exc:
        return RootReport(root.id, path, "refused", str(exc))
    if path in taken:
        return RootReport(root.id, path, "refused", "another root folder has this path")
    taken.add(path)
    try:
        status = os.stat(path)
    except OSError as exc:
        return RootReport(root.id, path, "refused", f"cannot be read: {exc.strerror or exc} (is it mounted in the gateway?)")
    if not stat.S_ISDIR(status.st_mode):
        return RootReport(root.id, path, "refused", "is not a directory")
    real = os.path.realpath(path)
    mount = mount_of(real, mounts)
    detail = dict(
        device=mount.device if mount else None, mount_id=mount.mount_id if mount else None,
        mount_point=mount.mount_point if mount else None, fs_type=mount.fs_type if mount else None,
    )
    writable = os.access(path, os.W_OK | os.X_OK)
    detail.update(writable=writable, free_bytes=free_bytes(path))
    owned = overlaps_own(path, own)
    if owned is not None:
        return RootReport(root.id, path, "refused", f"it overlaps the gateway's own folder {owned}", **detail)
    problem = volume_problem(mount)
    if problem is not None:
        return RootReport(root.id, path, "refused", problem, **detail)
    if not writable:
        return RootReport(root.id, path, "refused", "is not writable by the gateway", **detail)
    return RootReport(root.id, path, "accepted", "", **detail)


def judge_workspace(workspace: str, accepted: Sequence[str]) -> str | None:
    """Why ``workspace`` cannot be the working folder, or ``None``: it lies inside an accepted root, a link on the way
    does not leave it, and it is not a file."""
    try:
        check_root(workspace)
    except PathRefused as exc:
        return str(exc)
    if len(workspace) > MAX_PATH:
        return "not a usable path"
    holders = [root for root in accepted if inside(workspace, root)]
    if not holders:
        return "it is not inside an accepted root folder" if accepted else "no root folder is accepted"
    existing = workspace
    while not os.path.lexists(existing):
        existing = posixpath.dirname(existing)
    if not os.path.isdir(existing):
        return f"'{existing}' is not a directory"
    real = os.path.realpath(existing)
    if not any(inside(real, os.path.realpath(root)) for root in holders):
        return "a link in the path leads out of the root folder"
    return None


def evaluate(declared: DeclaredFolders, mounts: Sequence[Mount], own: Sequence[str]) -> FolderState:
    """What the gateway accepts of ``declared`` now (nothing is cached: a mount can appear or go away)."""
    taken: set[str] = set()
    reports = tuple(judge_root(root, mounts, own, taken) for root in declared.roots)
    accepted = [report.path for report in reports if report.state == "accepted"]
    workspace = declared.workspace
    if workspace is None:
        return FolderState(declared, reports)
    problem = judge_workspace(workspace, accepted)
    if problem is not None:
        return FolderState(declared, reports, None, "refused", problem)
    return FolderState(declared, reports, workspace, "accepted")


# ------------------------------------------------------------------
# inspect and browse
# ------------------------------------------------------------------


def _absolute(path: object) -> str:
    if not isinstance(path, str) or not path or "\0" in path or len(path) > MAX_PATH:
        raise PathRefused("a path is required")
    if not path.startswith("/") or posixpath.normpath(path) != path:
        raise PathRefused("not an absolute, normalized path (no '..', '//' or trailing '/')")
    return path


def write_test(path: str) -> dict:
    """The explicit Check of a root: create a hidden file in ``path`` and remove it. ``{"ok": bool, "error": str}``."""
    probe = posixpath.join(path, f".musizeer-write-check-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        with open(probe, "xb") as handle:
            handle.write(b"x")
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(probe)
    except OSError as exc:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return {"ok": False, "error": exc.strerror or str(exc)}
    return {"ok": True, "error": ""}


def inspect(path: object, mounts: Sequence[Mount], own: Sequence[str], check_write: bool = False) -> dict:
    """What the gateway sees at ``path``: whether it exists, is a directory, is writable, which mount holds it, whether
    that mount is a mounted volume (``mounted``) and the free space. Nothing about its content. With ``check_write`` (the
    administrator's explicit Check) it also creates and removes a hidden file in a folder (``write_test``): the only thing
    this operation ever writes."""
    path = _absolute(path)
    reply: dict = {
        "path": path, "exists": False, "directory": False, "writable": False, "mounted": False, "reason": "",
        "device": None, "mount_id": None, "mount_point": None, "fs_type": None, "free_bytes": None, "real_path": None,
    }
    try:
        status = os.stat(path)
    except OSError as exc:
        reply["reason"] = f"cannot be read: {exc.strerror or exc}"
        return reply
    real = os.path.realpath(path)
    mount = mount_of(real, mounts)
    problem = volume_problem(mount)
    owned = overlaps_own(path, own)
    reply.update(
        exists=True, directory=stat.S_ISDIR(status.st_mode), real_path=real,
        writable=os.access(path, os.W_OK | os.X_OK) if stat.S_ISDIR(status.st_mode) else False,
        mounted=problem is None and owned is None,
        device=mount.device if mount else None, mount_id=mount.mount_id if mount else None,
        mount_point=mount.mount_point if mount else None, fs_type=mount.fs_type if mount else None,
        free_bytes=free_bytes(path),
    )
    if check_write and reply["directory"] and owned is None:
        reply["write_test"] = write_test(path)
        if not reply["write_test"]["ok"]:
            reply["writable"] = False
    if not reply["directory"]:
        reply["reason"] = "is not a directory"
    elif owned is not None:
        reply["reason"] = f"it overlaps the gateway's own folder {owned}"
    elif problem is not None:
        reply["reason"] = problem
    elif not reply["writable"]:
        reply["reason"] = "is not writable by the gateway"
    return reply


def volumes(mounts: Sequence[Mount], own: Sequence[str]) -> list[Mount]:
    """The mounted volumes a folder can be picked in: directories mounted into the container that are not the container's
    own file system, not a kernel or memory file system and not the gateway's own folders. Listed once per mount point."""
    found: dict[str, Mount] = {}
    for mount in mounts:
        if volume_problem(mount) is not None or not os.path.isdir(mount.mount_point):
            continue
        if within_own(mount.mount_point, own) is not None:
            continue
        found[mount.mount_point] = mount  # a mount laid over another: the last one is the visible one
    return [found[point] for point in sorted(found)]


def browse(path: object, mounts: Sequence[Mount], own: Sequence[str]) -> dict:
    """The subdirectories of ``path``, which has to lie inside a mounted volume. With no path (``None`` or ``""``), the
    mounted volumes themselves. Never a file; a link whose target is not a directory of the SAME volume is not listed,
    and a ``path`` that leads out of its volume through a link is refused. :class:`PathRefused` for a path that cannot be
    browsed."""
    if path is None or path == "":
        listed = volumes(mounts, own)
        return {
            "path": None, "parent": None, "volume": None, "truncated": False,
            "directories": [
                {"name": mount.mount_point, "path": mount.mount_point, "device": mount.device, "mount_id": mount.mount_id,
                 "fs_type": mount.fs_type, "free_bytes": free_bytes(mount.mount_point)}
                for mount in listed
            ],
        }
    path = _absolute(path)
    nominal = mount_of(path, mounts)
    problem = volume_problem(nominal)
    if problem is not None:
        raise PathRefused(problem)
    assert nominal is not None
    if within_own(path, own) is not None:
        raise PathRefused("it is the gateway's own folder")
    try:
        real = os.path.realpath(path)
        if not stat.S_ISDIR(os.stat(real).st_mode):
            raise PathRefused("is not a directory")
    except OSError as exc:
        raise PathRefused(f"cannot be read: {exc.strerror or exc}") from exc
    if not inside(real, os.path.realpath(nominal.mount_point)):
        raise PathRefused("a link in the path leads out of the volume")
    volume_real = os.path.realpath(nominal.mount_point)
    entries: list[dict] = []
    truncated = False
    try:
        with os.scandir(real) as scan:
            for entry in scan:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    if entry.is_symlink() and not inside(os.path.realpath(entry.path), volume_real):
                        continue
                except OSError:
                    continue
                child = posixpath.join(path, entry.name)
                if within_own(child, own) is not None:
                    continue
                entries.append({"name": entry.name, "path": child, "link": entry.is_symlink()})
                if len(entries) >= MAX_BROWSE_ENTRIES:
                    truncated = True
                    break
    except OSError as exc:
        raise PathRefused(f"cannot be listed: {exc.strerror or exc}") from exc
    entries.sort(key=lambda item: item["name"].lower())
    parent = None if path == nominal.mount_point else posixpath.dirname(path)
    return {
        "path": path, "parent": parent, "truncated": truncated, "directories": entries,
        "volume": {"mount_point": nominal.mount_point, "fs_type": nominal.fs_type, "device": nominal.device, "mount_id": nominal.mount_id},
    }
