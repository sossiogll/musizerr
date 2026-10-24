"""Unpacking layers (hostile ones included) and composing a root filesystem from them."""
from __future__ import annotations

import os
import stat

import pytest

from musizeer_gateway.layers import LayerError, LayerStore, apply_layer, compose_rootfs, unpack_layer
from musizeer_gateway.oci import Layer
from support import LAYER_GZIP, Tar, digest

GZIP = LAYER_GZIP
PLAIN = "application/vnd.oci.image.layer.v1.tar"


def unpack(tmp_path, tar: Tar, name: str = "layer", gz: bool = True):
    blob = tmp_path / (name + ".tar")
    blob.write_bytes(tar.gzipped() if gz else tar.raw())
    dest = tmp_path / name
    dest.mkdir()
    unpack_layer(blob, GZIP if gz else PLAIN, dest)
    return dest


def test_files_directories_and_links_are_unpacked_with_safe_modes(tmp_path):
    tar = (
        Tar().dir("usr", 0o755).dir("usr/bin").file("usr/bin/tool", b"#!/bin/sh\n", 0o4755)
        .file("etc/conf", b"c", 0o000).symlink("bin", "usr/bin").symlink("python", "/usr/bin/python3")
        .hardlink("usr/bin/tool2", "usr/bin/tool").file("usr/data", b"d", 0o2644)
    )

    root = unpack(tmp_path, tar)

    assert (root / "usr/bin/tool").read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE((root / "usr/bin/tool").stat().st_mode) == 0o755, "setuid is dropped"
    assert stat.S_IMODE((root / "usr/data").stat().st_mode) == 0o644, "setgid is dropped"
    assert (root / "etc/conf").stat().st_mode & 0o400, "the owner can always read what was unpacked"
    assert os.readlink(root / "bin") == "usr/bin" and os.readlink(root / "python") == "/usr/bin/python3"
    assert os.path.samestat(os.stat(root / "usr/bin/tool"), os.stat(root / "usr/bin/tool2"))
    assert (root / "usr/bin/tool").stat().st_mtime == 1_700_000_000, "mtimes are kept (bytecode is validated by them)"


def test_an_uncompressed_layer_is_unpacked_too(tmp_path):
    assert (unpack(tmp_path, Tar().file("a/b", b"1"), gz=False) / "a/b").read_bytes() == b"1"


@pytest.mark.parametrize("build, message", [
    (lambda: Tar().file("/etc/passwd"), "absolute path"),
    (lambda: Tar().file("../escape"), "climbs out"),
    (lambda: Tar().file("a/../../escape"), "climbs out"),
    (lambda: Tar().device("dev/null"), "device node"),
    (lambda: Tar().fifo("run/pipe"), "device node"),
    (lambda: Tar().symlink("link", "../../etc/passwd"), "escapes the root"),
    (lambda: Tar().symlink("a/link", "../../x"), "escapes the root"),
    (lambda: Tar().hardlink("copy", "missing"), "does not hold"),
    (lambda: Tar().symlink("etc", "/etc").file("etc/passwd", b"pwned"), "goes through a link"),
    (lambda: Tar().symlink("d", "x").file("d/y"), "goes through a link"),
    (lambda: Tar().symlink("l", "target").hardlink("h", "l"), "regular file"),
    (lambda: Tar().hardlink("h", "/etc/passwd"), "absolute path"),
    (lambda: Tar().hardlink("h", "../../etc/passwd"), "climbs out"),
])
def test_a_hostile_layer_is_refused(tmp_path, build, message):
    with pytest.raises(LayerError, match=message):
        unpack(tmp_path, build())
    assert not any(p.name == "pwned" for p in tmp_path.rglob("*")), "nothing was written outside"


def test_nothing_is_written_through_a_link_to_the_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(LayerError):
        unpack(tmp_path, Tar().symlink("escape", str(outside)).file("escape/owned", b"x"))

    assert list(outside.iterdir()) == []


def test_a_layer_that_is_not_the_archive_it_claims_to_be_is_refused(tmp_path):
    blob = tmp_path / "bad"
    blob.write_bytes(b"this is not gzip")
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(LayerError):
        unpack_layer(blob, GZIP, dest)


def test_the_uncompressed_layer_must_hash_to_the_diff_id_the_image_declares(tmp_path):
    tar = Tar().file("a", b"1")
    blob = tmp_path / "blob"
    blob.write_bytes(tar.gzipped())
    good = digest(tar.raw())

    for number, diff_id in enumerate((good, "sha256:" + "0" * 64)):
        dest = tmp_path / f"out{number}"
        dest.mkdir()
        if diff_id == good:
            unpack_layer(blob, GZIP, dest, diff_id)
        else:
            with pytest.raises(LayerError, match="not to the"):
                unpack_layer(blob, GZIP, dest, diff_id)


def test_the_store_unpacks_a_layer_once_and_leaves_nothing_behind_on_failure(tmp_path):
    tar = Tar().file("a", b"1")
    data = tar.gzipped()
    layer = Layer(digest(data), len(data), GZIP)
    fetches = []

    def download(item, out):
        fetches.append(item.digest)
        out.write(data)

    store = LayerStore(tmp_path / "layers")
    first = store.ensure(layer, digest(tar.raw()), download)
    second = store.ensure(layer, digest(tar.raw()), download)

    assert first == second == store.path(layer.digest) and (first / "a").read_bytes() == b"1"
    assert fetches == [layer.digest], "a cached layer is not fetched again"

    bad = Layer("sha256:" + "9" * 64, 3, GZIP)
    with pytest.raises(LayerError):
        store.ensure(bad, "sha256:" + "0" * 64, lambda item, out: out.write(b"bad"))
    assert not store.path(bad.digest).exists()
    assert sorted(p.name for p in (tmp_path / "layers").iterdir()) == [store.path(layer.digest).name]


def layer(tmp_path, name: str, tar: Tar):
    return unpack(tmp_path, tar, name)


def test_layers_are_stacked_in_order_and_a_later_file_replaces_an_earlier_one(tmp_path):
    base = layer(tmp_path, "base", Tar().file("etc/os", b"base").file("etc/keep", b"keep").dir("srv"))
    top = layer(tmp_path, "top", Tar().file("etc/os", b"top").file("app/main", b"m"))

    root = tmp_path / "root"
    compose_rootfs([base, top], root)

    assert (root / "etc/os").read_bytes() == b"top"
    assert (root / "etc/keep").read_bytes() == b"keep" and (root / "app/main").read_bytes() == b"m"
    assert (root / "srv").is_dir()
    assert (base / "etc/os").read_bytes() == b"base", "the cached layer is untouched"


def test_a_whiteout_removes_what_a_lower_layer_put_there(tmp_path):
    base = layer(tmp_path, "base", Tar().file("etc/a", b"a").file("etc/b", b"b").file("opt/x/deep", b"d").file("gone", b"g"))
    top = layer(tmp_path, "top", Tar().whiteout("etc/a").whiteout("opt/x").whiteout("gone").file("gone.new", b"n"))

    root = tmp_path / "root"
    compose_rootfs([base, top], root)

    assert not (root / "etc/a").exists() and not (root / "opt/x").exists() and not (root / "gone").exists()
    assert (root / "etc/b").exists() and (root / "gone.new").exists()
    assert not any(p.name.startswith(".wh.") for p in root.rglob("*")), "whiteout markers are not part of the file system"


def test_an_opaque_directory_hides_everything_below_it(tmp_path):
    base = layer(tmp_path, "base", Tar().file("data/old1", b"1").file("data/sub/old2", b"2").file("other/keep", b"k"))
    top = layer(tmp_path, "top", Tar().dir("data").opaque("data").file("data/new", b"n"))

    root = tmp_path / "root"
    compose_rootfs([base, top], root)

    assert sorted(p.name for p in (root / "data").iterdir()) == ["new"]
    assert (root / "other/keep").exists()


def test_a_file_can_replace_a_directory_and_a_directory_a_file(tmp_path):
    base = layer(tmp_path, "base", Tar().file("a/inner", b"i").file("b", b"file"))
    top = layer(tmp_path, "top", Tar().file("a", b"now a file").file("b/inner", b"now a dir"))

    root = tmp_path / "root"
    compose_rootfs([base, top], root)

    assert (root / "a").is_file() and (root / "b/inner").read_bytes() == b"now a dir"


def test_a_symlink_to_a_directory_is_copied_as_a_link_not_followed(tmp_path):
    base = layer(tmp_path, "base", Tar().file("usr/lib/x", b"x").symlink("lib", "usr/lib"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("v")
    top = layer(tmp_path, "top", Tar().symlink("lib", "/somewhere/else"))

    root = tmp_path / "root"
    compose_rootfs([base, top], root)

    assert os.readlink(root / "lib") == "/somewhere/else" and (root / "usr/lib/x").exists()
    assert (outside / "victim").exists()


def test_layers_are_shared_between_images_as_hard_links(tmp_path):
    base = layer(tmp_path, "base", Tar().file("lib/big", b"b" * 1000))

    one, two = tmp_path / "one", tmp_path / "two"
    compose_rootfs([base], one)
    compose_rootfs([base], two)

    assert os.path.samestat(os.stat(one / "lib/big"), os.stat(base / "lib/big"))
    assert os.path.samestat(os.stat(two / "lib/big"), os.stat(base / "lib/big"))


def test_the_mount_points_the_sandbox_needs_exist_in_the_composed_root(tmp_path):
    base = layer(tmp_path, "base", Tar().dir("tmp").file("usr/x"))

    root = tmp_path / "root"
    compose_rootfs([base], root, ("proc", "tmp", "run/provider", "workspace"))

    assert all((root / name).is_dir() for name in ("proc", "tmp", "run/provider", "workspace"))
    with pytest.raises(LayerError, match="is a link"):
        compose_rootfs([layer(tmp_path, "evil", Tar().symlink("proc", "/etc"))], tmp_path / "root2", ("proc",))


def test_apply_layer_keeps_the_modes_of_directories_usable(tmp_path):
    base = layer(tmp_path, "base", Tar().dir("private", 0o000).file("private/f", b"1"))

    root = tmp_path / "root"
    root.mkdir()
    apply_layer(base, root)

    assert (root / "private").stat().st_mode & 0o700 == 0o700
