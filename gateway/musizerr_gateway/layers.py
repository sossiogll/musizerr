"""
Layers: unpacking them safely, caching them by digest, and composing an image's
root filesystem from them.

**A layer is untrusted data.** It is extracted by hand, never with ``extractall``:

- a member's path must be relative, free of ``..`` and of NUL, and no component of
  its parent path may be a link (nothing is ever written through a link);
- symbolic links may point anywhere INSIDE the image (an absolute target is read
  inside the sandbox's own root, as every image's ``/usr/bin/python`` is), but a
  relative target that climbs above the root is refused;
- a hard link must point at a regular file this layer already extracted;
- device nodes, FIFOs and anything else that is not a file, directory or link are
  refused;
- setuid, setgid and sticky bits are dropped, and the unpacked size is bounded.

Whiteouts (``.wh.<name>``, and ``.wh..wh..opq`` for an opaque directory) are kept
as inert files in the layer's own directory and applied when the root filesystem
is composed.

**Layers are shared.** Each is unpacked once into ``layers/<sha256>/`` and an image's
root filesystem is made of hard links to the files of its layers: two images with
the same base layers store them once.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
import threading
import uuid
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from .oci import GZIP_LAYER_TYPES, ImageError, Layer

WHITEOUT_PREFIX = ".wh."
OPAQUE = ".wh..wh..opq"
MAX_UNPACKED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 1_000_000


class LayerError(ImageError):
    """A layer that cannot be unpacked safely."""


# ------------------------------------------------------------------
# Unpacking
# ------------------------------------------------------------------


class _HashingReader:
    """Reads a stream and hashes what passes (the digest of the uncompressed layer)."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self.digest.update(data)
        return data


def _clean_parts(name: str) -> tuple[str, ...]:
    if "\x00" in name:
        raise LayerError("a path contains a NUL byte")
    if name.startswith("/"):
        raise LayerError(f"absolute path '{name}'")
    parts = tuple(part for part in PurePosixPath(name).parts if part != ".")
    if ".." in parts:
        raise LayerError(f"path '{name}' climbs out of the layer")
    return parts


def _file_mode(mode: int) -> int:
    return (mode & 0o777) | 0o400


def _dir_mode(mode: int) -> int:
    return (mode & 0o777) | 0o700


def _ensure_parents(root: Path, parts: tuple[str, ...]) -> Path:
    """The directory that must hold ``parts[-1]``: created when absent, and never
    reached through a link."""
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            continue
        if not stat.S_ISDIR(mode):
            raise LayerError(f"'{'/'.join(parts)}' goes through a link or a file")
    return current


def _remove(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _check_symlink(parts: tuple[str, ...], target: str) -> None:
    if "\x00" in target:
        raise LayerError(f"link '{'/'.join(parts)}' has a NUL byte in its target")
    if target.startswith("/"):
        return
    resolved = posixpath.normpath(posixpath.join(*parts[:-1], target)) if len(parts) > 1 else posixpath.normpath(target)
    if resolved == ".." or resolved.startswith("../"):
        raise LayerError(f"link '{'/'.join(parts)}' -> '{target}' escapes the root")


def unpack_layer(blob: Path, media_type: str, dest: Path, diff_id: str | None = None) -> None:
    """Unpack the layer in ``blob`` into the (new, empty) directory ``dest``. When
    ``diff_id`` is given the uncompressed layer must hash to it."""
    total = 0
    count = 0
    with open(blob, "rb") as raw:
        stream: BinaryIO = gzip.GzipFile(fileobj=raw) if media_type in GZIP_LAYER_TYPES else raw  # type: ignore[assignment]
        hashing = _HashingReader(stream)
        try:
            tar = tarfile.open(fileobj=hashing, mode="r|")  # type: ignore[call-overload]
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise LayerError(f"the layer is not a tar archive: {exc}") from exc
        with tar:
            try:
                for member in tar:
                    count += 1
                    if count > MAX_MEMBERS:
                        raise LayerError("the layer has too many entries")
                    total += _extract(tar, member, dest, MAX_UNPACKED_BYTES - total)
            except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
                raise LayerError(f"the layer cannot be read: {exc}") from exc
        while hashing.read(1024 * 1024):  # the end of the archive: the digest covers all of it
            pass
    if diff_id is not None and "sha256:" + hashing.digest.hexdigest() != diff_id:
        raise LayerError(f"the layer unpacks to sha256:{hashing.digest.hexdigest()}, not to the {diff_id} its image declares")


def _extract(tar: tarfile.TarFile, member: tarfile.TarInfo, root: Path, budget: int) -> int:
    parts = _clean_parts(member.name)
    if not parts:
        return 0  # the root directory itself
    name = parts[-1]
    if name.startswith(WHITEOUT_PREFIX + WHITEOUT_PREFIX) and name != OPAQUE:
        return 0  # an aufs private entry: not part of the file system
    if member.isdev() or member.isfifo() or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise LayerError(f"'{member.name}' is a device node or a special file")
    parent = _ensure_parents(root, parts)
    path = parent / name

    if member.isdir():
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            os.mkdir(path, _dir_mode(member.mode))
        else:
            if not stat.S_ISDIR(mode):
                _remove(path)
                os.mkdir(path, _dir_mode(member.mode))
        os.chmod(path, _dir_mode(member.mode))
        return 0
    if member.issym():
        _check_symlink(parts, member.linkname)
        _remove(path)
        os.symlink(member.linkname, path)
        return 0
    if member.islnk():
        target_parts = _clean_parts(member.linkname)
        if not target_parts:
            raise LayerError(f"hard link '{member.name}' has no target")
        target_parent = _ensure_parents(root, target_parts)
        source = target_parent / target_parts[-1]
        try:
            source_mode = os.lstat(source).st_mode
        except FileNotFoundError:
            raise LayerError(f"hard link '{member.name}' points at '{member.linkname}', which the layer does not hold") from None
        if not stat.S_ISREG(source_mode):
            raise LayerError(f"hard link '{member.name}' does not point at a regular file")
        _remove(path)
        os.link(source, path)
        return 0
    # a regular file
    if member.size > budget:
        raise LayerError("the layer unpacks to more than the size limit")
    _remove(path)
    source = tar.extractfile(member)
    if source is None:
        raise LayerError(f"'{member.name}' has no content")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)
    os.chmod(path, _file_mode(member.mode))
    os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)
    return member.size


# ------------------------------------------------------------------
# The cache
# ------------------------------------------------------------------


class LayerStore:
    """Unpacked layers, by digest. A layer is unpacked once and shared by every
    image that has it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def path(self, digest: str) -> Path:
        return self.root / digest.replace(":", "-")

    def _lock(self, digest: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(digest, threading.Lock())

    def ensure(self, layer: Layer, diff_id: str, download: Callable[[Layer, BinaryIO], None]) -> Path:
        final = self.path(layer.digest)
        with self._lock(layer.digest):
            if final.is_dir():
                return final
            self.root.mkdir(parents=True, exist_ok=True)
            staging = self.root / f".unpack-{uuid.uuid4().hex}"
            blob = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, prefix=".blob-", delete=False) as out:
                    blob = Path(out.name)
                    download(layer, out)
                staging.mkdir()
                unpack_layer(blob, layer.media_type, staging, diff_id)
                os.rename(staging, final)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                if blob is not None:
                    blob.unlink(missing_ok=True)
        return final


# ------------------------------------------------------------------
# Composing a root filesystem
# ------------------------------------------------------------------


def _make_dir(path: Path, like: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        os.mkdir(path)
    else:
        if not stat.S_ISDIR(mode):
            os.unlink(path)
            os.mkdir(path)
    os.chmod(path, stat.S_IMODE(os.lstat(like).st_mode) | 0o700)


def apply_layer(layer_dir: Path, rootfs: Path) -> None:
    """Add one layer on top of what ``rootfs`` holds: whiteouts remove what lower
    layers put there, everything else is hard-linked (symlinks are copied)."""
    for current, dirnames, filenames in os.walk(layer_dir, followlinks=False):
        relative = Path(current).relative_to(layer_dir)
        target_dir = rootfs / relative
        _make_dir(target_dir, Path(current))
        # A symlink to a directory is listed with the directories: it is a file here.
        links = [name for name in dirnames if os.path.islink(os.path.join(current, name))]
        dirnames[:] = [name for name in dirnames if name not in links]
        entries = filenames + links
        if OPAQUE in filenames:
            for child in list(target_dir.iterdir()):
                _remove(child)
        for name in entries:
            if name.startswith(WHITEOUT_PREFIX):
                if name != OPAQUE and not name.startswith(WHITEOUT_PREFIX + WHITEOUT_PREFIX):
                    _remove(target_dir / name[len(WHITEOUT_PREFIX):])
                continue
        for name in entries:
            if name.startswith(WHITEOUT_PREFIX):
                continue
            source, destination = Path(current) / name, target_dir / name
            _remove(destination)
            if os.path.islink(source):
                os.symlink(os.readlink(source), destination)
            else:
                os.link(source, destination)


def compose_rootfs(layer_dirs: list[Path], dest: Path, mountpoints: tuple[str, ...] = ()) -> None:
    """Build the root filesystem of an image in the new directory ``dest`` from its
    layers (base first). ``mountpoints`` are the empty directories the sandbox mounts
    over (the layers need not have them)."""
    dest.mkdir(parents=True)
    for layer_dir in layer_dirs:
        apply_layer(layer_dir, dest)
    for mountpoint in mountpoints:
        current = dest
        for part in PurePosixPath(mountpoint).parts:
            current = current / part
            if os.path.islink(current):
                raise LayerError(f"the mount point '{mountpoint}' is a link in the image")
            if not current.exists():
                os.mkdir(current, 0o755)
