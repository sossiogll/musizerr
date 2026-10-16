"""Which host folders a sandbox may be given, and the mount points they need."""
from __future__ import annotations

from pathlib import Path

import pytest

from musizerr_gateway.config import ConfigError, from_environment
from musizerr_gateway.filesystem import Bind, PathRefused, check_root, inside, make_mountpoint, validate_grant


@pytest.fixture
def library(tmp_path):
    """A library root with a music folder, a state folder and a workspace directory next to the music."""
    root = tmp_path / "lib"
    (root / "music").mkdir(parents=True)
    (root / ".work").mkdir()
    (tmp_path / "state").mkdir()
    return root


def grant(value, library, mode="rw", **kwargs):
    kwargs.setdefault("forbidden", ())
    kwargs.setdefault("workspace", str(library / ".work"))
    kwargs.setdefault("reserved", ())  # pytest's temporary folders live under /tmp, which a sandbox mounts itself
    return validate_grant(value, mode, [str(library)], **kwargs)


# ------------------------------------------------------------------
# What is granted
# ------------------------------------------------------------------


def test_a_folder_inside_a_root_is_bound_at_the_same_path_with_the_mode_of_the_manifest(library):
    bind = grant(str(library / "music"), library, "ro")

    assert bind == Bind(source=str(library / "music"), dest=str(library / "music"), mode="ro")


def test_the_root_itself_may_be_granted_and_its_workspace_directory_is_covered_by_an_empty_tmpfs(library):
    bind = grant(str(library), library)

    assert bind.dest == str(library) and bind.masks == (str(library / ".work"),)


def test_a_link_that_stays_inside_the_root_is_bound_by_what_it_resolves_to(library):
    (library / "alias").symlink_to(library / "music")

    bind = grant(str(library / "alias"), library)

    assert bind.dest == str(library / "alias") and bind.source == str((library / "music").resolve())


# ------------------------------------------------------------------
# What is refused
# ------------------------------------------------------------------


@pytest.mark.parametrize("value, message", [
    (None, "no path"), ("", "no path"), (5, "no path"), (["/a"], "no path"),
    ("music", "absolute"), ("./music", "absolute"), ("/", "absolute"),
    ("LIB/music/", "absolute"), ("LIB/music/../music", "absolute"), ("LIB//music", "absolute"), ("LIB/./music", "absolute"),
    ("/etc", "inside a folder"), ("/", "absolute"), ("LIB/../outside", "absolute"),
    ("LIB/missing", "cannot be read"),
])
def test_a_path_that_is_not_a_plain_folder_inside_a_root_is_refused(library, value, message):
    if isinstance(value, str):
        value = value.replace("LIB", str(library))

    with pytest.raises(PathRefused, match=message):
        grant(value, library)


def test_a_file_is_not_a_folder(library):
    (library / "song.flac").write_text("x")

    with pytest.raises(PathRefused, match="not a directory"):
        grant(str(library / "song.flac"), library)


def test_the_name_of_a_root_is_not_enough_a_sibling_with_the_same_prefix_is_outside_it(library):
    sibling = Path(str(library) + "-evil")
    sibling.mkdir()

    with pytest.raises(PathRefused, match="inside a folder"):
        grant(str(sibling), library)


def test_a_link_inside_the_root_that_leads_out_is_refused(library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (library / "escape").symlink_to(outside)
    (library / "music" / "deeper").symlink_to(outside)

    for link in (library / "escape", library / "music" / "deeper"):
        with pytest.raises(PathRefused, match="leads out"):
            grant(str(link), library)


def test_a_link_to_the_root_of_the_host_is_refused(library):
    (library / "everything").symlink_to("/")

    with pytest.raises(PathRefused, match="leads out"):
        grant(str(library / "everything"), library)


def test_without_roots_nothing_is_granted(library):
    with pytest.raises(PathRefused, match="GATEWAY_FILESYSTEM_ROOTS is empty"):
        validate_grant(str(library), "rw", [], (), str(library / ".work"), reserved=())


def test_the_gateways_own_state_is_never_granted_nor_what_holds_it(library, tmp_path):
    state = library / "state"
    state.mkdir()

    with pytest.raises(PathRefused, match="own state"):
        grant(str(state), library, forbidden=[str(state)])
    with pytest.raises(PathRefused, match="own state"):
        grant(str(library), library, forbidden=[str(state)])


def test_the_workspace_directory_and_what_is_inside_it_are_not_granted(library):
    (library / ".work" / "other").mkdir()

    for path in (library / ".work", library / ".work" / "other"):
        with pytest.raises(PathRefused, match="inside the workspace"):
            grant(str(path), library)


def test_what_every_sandbox_mounts_itself_cannot_be_granted(library):
    with pytest.raises(PathRefused, match="collides with"):
        grant(str(library / "music"), library, reserved=[str(library / "music")])
    with pytest.raises(PathRefused, match="collides with"):
        grant(str(library / "music"), library, reserved=[str(library)])
    with pytest.raises(PathRefused, match="collides with"):
        validate_grant("/tmp/x", "rw", ["/tmp"], (), "/downloads/.work")  # the real list: /tmp is private to each sandbox


@pytest.mark.parametrize("root", ["/", "", "downloads", "/downloads/", "/downloads/../etc", "/proc", "/tmp/x", "/run/musizerr", "/dev", "/"])
def test_a_root_that_could_expose_the_gateway_is_not_a_root(root):
    with pytest.raises(PathRefused):
        check_root(root)


def test_inside_means_the_folder_or_below_it_never_a_longer_name():
    assert inside("/downloads", "/downloads") and inside("/downloads/a", "/downloads")
    assert not inside("/downloads-evil", "/downloads") and not inside("/download", "/downloads")


# ------------------------------------------------------------------
# Mount points in the image's root filesystem
# ------------------------------------------------------------------


def test_mount_points_are_made_in_the_image_without_touching_what_is_there(tmp_path):
    rootfs = tmp_path / "rootfs"
    (rootfs / "downloads").mkdir(parents=True)
    (rootfs / "downloads" / "keep").write_text("x")

    make_mountpoint(rootfs, "/downloads/music/deep")

    assert (rootfs / "downloads/music/deep").is_dir() and (rootfs / "downloads/keep").read_text() == "x"
    make_mountpoint(rootfs, "/downloads/music/deep")  # again: nothing to do


def test_a_link_in_the_image_is_never_followed_to_make_a_mount_point(tmp_path):
    rootfs, host = tmp_path / "rootfs", tmp_path / "host"
    rootfs.mkdir()
    host.mkdir()
    (rootfs / "downloads").symlink_to(host)  # an absolute-looking way out of the image

    with pytest.raises(PathRefused, match="a link or a file"):
        make_mountpoint(rootfs, "/downloads/music")
    assert list(host.iterdir()) == [], "nothing was created on the gateway's own filesystem"


def test_a_file_in_the_image_cannot_be_a_mount_point(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "downloads").write_text("x")

    with pytest.raises(PathRefused):
        make_mountpoint(rootfs, "/downloads/music")


# ------------------------------------------------------------------
# The configuration
# ------------------------------------------------------------------


def test_the_roots_come_from_the_environment():
    assert from_environment({}).filesystem_roots == ()
    config = from_environment({"GATEWAY_FILESYSTEM_ROOTS": "/downloads:/music/library: :/downloads"})
    assert config.filesystem_roots == ("/downloads", "/music/library")


@pytest.mark.parametrize("value", ["/", "relative", "/downloads/../etc", "/proc/1", "/var/lib/musizerr-gateway", "/var/lib/musizerr-gateway/x", "/var"])
def test_a_root_that_could_expose_the_gateway_stops_it_from_starting(value):
    with pytest.raises(ConfigError, match="GATEWAY_FILESYSTEM_ROOTS"):
        from_environment({"GATEWAY_FILESYSTEM_ROOTS": value})


def test_a_folder_next_to_the_roots_is_not_a_reason_to_refuse_them():
    assert from_environment({"GATEWAY_FILESYSTEM_ROOTS": "/var/lib/other"}).filesystem_roots == ("/var/lib/other",)
