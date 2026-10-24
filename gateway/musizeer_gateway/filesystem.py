"""
Host paths a sandbox may be given (``filesystem_from_settings``).

A provider that declares ``filesystem_from_settings`` names SETTINGS whose value is a folder of the
host (local storage's root directory). Only the host knows the values, so it sends the CURRENT ones with
the egress grant (``control.py``); the gateway decides here whether each may be bound into the sandbox.
A path is bound ONLY if all of this holds:

- it is absolute and normalized (no ``..``, no ``//``, no trailing ``/``) and is not ``/``;
- it lies inside one of the roots the operator configured (``MUSIZEER_GATEWAY_FILESYSTEM_ROOTS``) and so does
  what it resolves to (a link inside the root that leads out of it is refused);
- it exists and is a directory;
- it is not, and does not hold, the gateway's own state or sockets, and it is not inside the workspace
  directory (a folder that HOLDS the workspace directory, such as the library root, is bound with the
  workspace directory covered by an empty ``tmpfs``: another provider's files are not for this one);
- it does not collide with what every sandbox mounts itself (``/proc``, ``/dev``, ``/tmp``, ``/run``,
  ``/workspace``).

The mode (``ro`` or ``rw``) is the one the VERIFIED image declares for that setting, never one the host
sent. The sandbox sees the folder at the same absolute path (``dest``); what is mounted is the RESOLVED
path (``source``), checked again just before every start.
"""
from __future__ import annotations

import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_PATH = 4096
# What every sandbox mounts by itself: a granted folder may neither be nor hold any of them.
RESERVED = ("/proc", "/dev", "/tmp", "/run", "/workspace")


class PathRefused(ValueError):
    """A path that must not be given to a sandbox; the message says why."""


@dataclass(frozen=True)
class Bind:
    source: str  # the resolved folder on the gateway's filesystem
    dest: str  # where the sandbox sees it: the granted path
    mode: str  # "ro" | "rw"
    masks: tuple[str, ...] = ()  # absolute paths inside the sandbox to cover with an empty tmpfs


def inside(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies under it (both absolute and normalized)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_root(root: str) -> str:
    """``root`` if it can be a root of grants; else :class:`PathRefused`."""
    if not isinstance(root, str) or not root.startswith("/") or "\0" in root or posixpath.normpath(root) != root or root == "/":
        raise PathRefused(f"'{root}' is not an absolute, normalized folder other than '/'")
    for reserved in RESERVED:
        if inside(root, reserved) or inside(reserved, root):
            raise PathRefused(f"'{root}' collides with {reserved}, which every sandbox mounts itself")
    return root


def validate_grant(
    value: object, mode: str, roots: Iterable[str], forbidden: Iterable[str], workspace: str,
    reserved: Iterable[str] | None = None,
) -> Bind:
    """The :class:`Bind` for the folder ``value`` names, or :class:`PathRefused`."""
    if not isinstance(value, str) or not value:
        raise PathRefused("the setting holds no path")
    if len(value) > MAX_PATH or "\0" in value:
        raise PathRefused("not a usable path")
    if not value.startswith("/") or posixpath.normpath(value) != value or value == "/":
        raise PathRefused("not an absolute, normalized path (no '..', '//' or trailing '/', and not '/')")
    roots = tuple(roots)
    if not any(inside(value, root) for root in roots):
        raise PathRefused("not inside a folder this gateway may grant" if roots else "this gateway grants no folder (MUSIZEER_GATEWAY_FILESYSTEM_ROOTS is empty)")
    for mounted in (RESERVED if reserved is None else reserved):
        if inside(value, mounted) or inside(mounted, value):
            raise PathRefused(f"collides with {mounted}, which every sandbox mounts itself")
    try:
        status = os.stat(value)
    except OSError as exc:
        raise PathRefused(f"cannot be read: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise PathRefused("is not a directory")
    real = os.path.realpath(value)
    real_roots = [os.path.realpath(root) for root in roots if os.path.isdir(root)]
    if not any(inside(real, root) for root in real_roots):
        raise PathRefused("a link in the path leads out of the folders this gateway may grant")
    for own in forbidden:
        own_real = os.path.realpath(own)
        if inside(real, own_real) or inside(own_real, real):
            raise PathRefused("it is, or holds, the gateway's own state")
    workspace_real = os.path.realpath(workspace)
    if inside(real, workspace_real):
        raise PathRefused("it is inside the workspace directory")
    masks: tuple[str, ...] = ()
    if inside(workspace_real, real):
        masks = (posixpath.join(value, workspace_real[len(real):].lstrip("/")),)
    return Bind(source=real, dest=value, mode=mode, masks=masks)


def make_mountpoint(rootfs: Path, dest: str) -> None:
    """Make sure the image's root filesystem has an (empty) directory at ``dest`` for bubblewrap to
    mount over, without ever following a link of the image: a link there would lead out of the root
    filesystem, onto the gateway's own."""
    current = rootfs
    for part in Path(dest).parts[1:]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            continue
        if not stat.S_ISDIR(mode):
            raise PathRefused(f"'{dest}' cannot be mounted: the image has a link or a file at '{current.relative_to(rootfs)}'")
