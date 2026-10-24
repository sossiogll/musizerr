"""The root folders the host declares and the rules the gateway applies to them (ADR-0008 §9): the mount table, the
acceptance of a root and of the working folder, ``inspect`` and ``browse``."""
from __future__ import annotations

import os

import pytest

from musizeer_gateway import filesystem, roots
from musizeer_gateway.filesystem import PathRefused
from musizeer_gateway.roots import DeclaredFolders, DeclaredRoot, FoldersError, Mount

MOUNTINFO = r"""
22 28 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw
23 28 0:22 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc rw
28 1 0:43 / / rw,relatime master:1 - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/ABC,upperdir=/x,workdir=/y
29 28 0:24 / /dev rw,nosuid shared:2 - tmpfs tmpfs rw,size=65536k,mode=755
31 28 8:17 /Musica-staging /downloads rw,relatime - ext4 /dev/sdb1 rw
32 28 8:17 /Musica /library rw,relatime - ext4 /dev/sdb1 rw
33 32 8:17 /Musica/My\040Music /library/with\040space rw,relatime - ext4 /dev/sdb1 rw
34 28 8:1 /var/lib/docker/containers/abc/hostname /etc/hostname rw,relatime - ext4 /dev/sda1 rw
35 28 0:50 / /mnt/ram rw - tmpfs tmpfs rw
not a mountinfo line
"""


@pytest.fixture(autouse=True)
def no_reserved(monkeypatch):
    # pytest's temporary folders live under /tmp, which a sandbox mounts itself (check_root refuses it)
    monkeypatch.setattr(filesystem, "RESERVED", ())


def table(*points, fs="ext4"):
    """A mount table with the container's own file system at ``/`` and ``points`` as mounted volumes."""
    mounts = [Mount(1, 0, "0:1", "/", "/", "overlay")]
    mounts += [Mount(10 + i, 1, f"8:{i}", "/", os.path.realpath(str(point)), fs) for i, point in enumerate(points)]
    return tuple(mounts)


# ------------------------------------------------------------------
# The mount table
# ------------------------------------------------------------------


def test_the_mountinfo_of_the_container_is_read_line_by_line_and_an_unreadable_line_is_skipped():
    mounts = roots.parse_mountinfo(MOUNTINFO)

    assert [m.mount_point for m in mounts] == ["/sys", "/proc", "/", "/dev", "/downloads", "/library", "/library/with space", "/etc/hostname", "/mnt/ram"]
    library = next(m for m in mounts if m.mount_point == "/library")
    assert (library.mount_id, library.device, library.root, library.fs_type, library.source) == (32, "8:17", "/Musica", "ext4", "/dev/sdb1")


def test_a_missing_mountinfo_is_no_mount_at_all_so_no_root_is_accepted(tmp_path):
    assert roots.read_mountinfo(str(tmp_path / "nope")) == ()
    assert roots.volume_problem(roots.mount_of("/library", ())) == "it is on no mount the container lists"


@pytest.mark.parametrize("path, expected", [
    ("/library", "/library"), ("/library/Artist/Album", "/library"), ("/library/with space/x", "/library/with space"),
    ("/downloads", "/downloads"), ("/library-other", "/"), ("/etc/hosts", "/"), ("/etc/hostname", "/etc/hostname"), ("/proc/1/status", "/proc"),
])
def test_a_path_belongs_to_the_longest_mount_point_that_contains_it_on_path_boundaries(path, expected):
    assert roots.mount_of(path, roots.parse_mountinfo(MOUNTINFO)).mount_point == expected


def test_of_two_mounts_at_one_point_the_last_one_is_the_visible_one():
    mounts = (Mount(1, 0, "0:1", "/", "/", "overlay"), Mount(2, 1, "8:1", "/", "/data", "ext4"), Mount(3, 2, "8:2", "/", "/data", "xfs"))

    assert roots.mount_of("/data/x", mounts).fs_type == "xfs"


@pytest.mark.parametrize("path, problem", [
    ("/etc", "part of the container's own file system"),
    ("/etc/ssl/certs", "part of the container's own file system"),
    ("/proc/1", "proc file system"),
    ("/dev/shm", "tmpfs file system"),
    ("/mnt/ram", "tmpfs file system"),
    ("/sys", "sysfs file system"),
    ("/downloads/x", None), ("/library", None), ("/etc/hostname", None),
])
def test_only_a_mounted_volume_is_a_place_for_a_root(path, problem):
    found = roots.volume_problem(roots.mount_of(path, roots.parse_mountinfo(MOUNTINFO)))

    assert (found is None) if problem is None else (problem in found)


# ------------------------------------------------------------------
# What the host declares
# ------------------------------------------------------------------


def test_the_message_of_the_host_is_read_into_roots_and_a_working_folder():
    declared = roots.parse_folders([{"id": "a", "path": "/downloads"}, {"id": "b.c-d_1", "path": "/library"}], "/downloads/.work")

    assert declared == DeclaredFolders((DeclaredRoot("a", "/downloads"), DeclaredRoot("b.c-d_1", "/library")), "/downloads/.work")
    assert roots.parse_folders(None, None) == DeclaredFolders()
    assert roots.parse_folders([], None).workspace is None


@pytest.mark.parametrize("raw, workspace", [
    ("x", None), ({}, None), ([1], None), ([{"id": "a"}], None), ([{"path": "/x"}], None), ([{"id": "a", "path": 5}], None),
    ([{"id": "", "path": "/x"}], None), ([{"id": "a b", "path": "/x"}], None), ([{"id": "x" * 65, "path": "/x"}], None),
    ([{"id": "a", "path": "/x"}, {"id": "a", "path": "/y"}], None),
    ([{"id": str(i), "path": f"/{i}"} for i in range(roots.MAX_ROOTS + 1)], None),
    ([], ""), ([], 5), ([], ["/x"]),
])
def test_folders_that_are_not_folders_refuse_the_whole_message(raw, workspace):
    with pytest.raises(FoldersError):
        roots.parse_folders(raw, workspace)


# ------------------------------------------------------------------
# Accepting a root
# ------------------------------------------------------------------


def evaluate(tmp_path, declared, mounts, own=()):
    return roots.evaluate(declared, mounts, own)


def only(state, root_id):
    return next(report for report in state.reports if report.id == root_id)


def test_a_writable_directory_on_a_mounted_volume_is_accepted_and_reports_its_mount_and_free_space(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()

    state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("lib", str(library)),)), table(library))

    report = only(state, "lib")
    assert report.state == "accepted" and report.reason == "" and report.writable is True
    assert (report.device, report.mount_point, report.fs_type) == ("8:0", os.path.realpath(str(library)), "ext4")
    assert isinstance(report.mount_id, int) and report.free_bytes is not None and report.free_bytes > 0
    assert state.accepted_roots == (str(library),)


def test_a_folder_inside_a_volume_is_accepted_too(tmp_path):
    (tmp_path / "vol" / "music").mkdir(parents=True)

    state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("m", str(tmp_path / "vol" / "music")),)), table(tmp_path / "vol"))

    assert only(state, "m").state == "accepted" and only(state, "m").mount_point == os.path.realpath(str(tmp_path / "vol"))


def test_a_root_that_is_part_of_the_containers_own_file_system_is_refused(tmp_path):
    (tmp_path / "image-dir").mkdir()

    state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("x", str(tmp_path / "image-dir")),)), table())

    assert only(state, "x").state == "refused" and "not a mounted volume" in only(state, "x").reason
    assert state.accepted_roots == ()


@pytest.mark.parametrize("fs", ["tmpfs", "proc", "sysfs", "devtmpfs", "cgroup2", "ramfs"])
def test_a_root_on_a_kernel_or_memory_file_system_is_refused(tmp_path, fs):
    (tmp_path / "ram").mkdir()

    state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("ram", str(tmp_path / "ram")),)), table(tmp_path / "ram", fs=fs))

    assert only(state, "ram").state == "refused" and f"{fs} file system" in only(state, "ram").reason


def test_etc_is_never_a_root_even_if_the_host_declares_it():
    state = roots.evaluate(DeclaredFolders((DeclaredRoot("etc", "/etc"),)), roots.parse_mountinfo(MOUNTINFO), ())

    assert only(state, "etc").state == "refused" and "not a mounted volume" in only(state, "etc").reason


@pytest.mark.parametrize("path, reason", [
    ("/", "other than '/'"), ("relative", "absolute"), ("/a/../b", "normalized"), ("/a/", "normalized"),
    ("/proc/x", "collides with /proc"), ("/dev", "collides with /dev"),
])
def test_a_path_that_cannot_be_a_root_is_refused_with_the_reason(path, reason, monkeypatch):
    monkeypatch.undo()  # the real list of what every sandbox mounts itself
    state = roots.evaluate(DeclaredFolders((DeclaredRoot("x", path),)), (), ())

    assert only(state, "x").state == "refused" and reason in only(state, "x").reason


def test_a_root_that_does_not_exist_or_is_a_file_is_refused(tmp_path):
    (tmp_path / "a-file").write_text("x")
    declared = DeclaredFolders((DeclaredRoot("missing", str(tmp_path / "missing")), DeclaredRoot("file", str(tmp_path / "a-file"))))

    state = evaluate(tmp_path, declared, table(tmp_path))

    assert "cannot be read" in only(state, "missing").reason and "is it mounted in the gateway" in only(state, "missing").reason
    assert only(state, "file").reason == "is not a directory"


def test_a_root_the_gateway_cannot_write_to_is_refused(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    library.chmod(0o555)
    try:
        if os.access(library, os.W_OK):
            pytest.skip("running as a user that ignores permissions (root)")
        state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("lib", str(library)),)), table(library))
    finally:
        library.chmod(0o755)

    assert only(state, "lib").state == "refused" and only(state, "lib").writable is False
    assert only(state, "lib").reason == "is not writable by the gateway"


def test_a_root_that_overlaps_the_gateways_own_folders_is_refused(tmp_path):
    state_dir = tmp_path / "vol" / "state"
    state_dir.mkdir(parents=True)
    declared = DeclaredFolders((DeclaredRoot("parent", str(tmp_path / "vol")), DeclaredRoot("inside", str(state_dir)), DeclaredRoot("same", str(state_dir))))

    state = evaluate(tmp_path, declared, table(tmp_path / "vol"), own=(str(state_dir),))

    assert state.accepted_roots == ()
    for root_id in ("parent", "inside"):
        assert "overlaps the gateway's own folder" in only(state, root_id).reason
    assert only(state, "same").reason == "another root folder has this path"


def test_the_same_path_twice_is_refused_but_one_root_may_lie_inside_another(tmp_path):
    (tmp_path / "vol" / "inner").mkdir(parents=True)
    declared = DeclaredFolders((
        DeclaredRoot("outer", str(tmp_path / "vol")), DeclaredRoot("inner", str(tmp_path / "vol" / "inner")),
        DeclaredRoot("again", str(tmp_path / "vol")),
    ))

    state = evaluate(tmp_path, declared, table(tmp_path / "vol"))

    assert only(state, "outer").state == "accepted" and only(state, "inner").state == "accepted"
    assert only(state, "again").state == "refused" and only(state, "again").reason == "another root folder has this path"


def test_a_link_is_judged_by_what_it_resolves_to(tmp_path):
    (tmp_path / "vol").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "vol" / "escape").symlink_to(tmp_path / "elsewhere")

    state = evaluate(tmp_path, DeclaredFolders((DeclaredRoot("escape", str(tmp_path / "vol" / "escape")),)), table(tmp_path / "vol"))

    assert only(state, "escape").state == "refused" and "not a mounted volume" in only(state, "escape").reason


# ------------------------------------------------------------------
# Accepting the working folder
# ------------------------------------------------------------------


def workspace_state(tmp_path, workspace, *declared_roots, mounts=None):
    return roots.evaluate(
        DeclaredFolders(tuple(declared_roots), workspace), table(tmp_path / "lib") if mounts is None else mounts, (),
    )


def test_the_working_folder_is_accepted_inside_an_accepted_root_even_before_it_exists(tmp_path):
    (tmp_path / "lib").mkdir()
    root = DeclaredRoot("lib", str(tmp_path / "lib"))

    state = workspace_state(tmp_path, str(tmp_path / "lib" / ".musizeer-work"), root)

    assert (state.workspace, state.workspace_state, state.workspace_reason) == (str(tmp_path / "lib" / ".musizeer-work"), "accepted", "")
    assert state.workspace_view() == {"path": str(tmp_path / "lib" / ".musizeer-work"), "state": "accepted", "reason": ""}


def test_the_working_folder_may_be_the_root_itself(tmp_path):
    (tmp_path / "lib").mkdir()

    state = workspace_state(tmp_path, str(tmp_path / "lib"), DeclaredRoot("lib", str(tmp_path / "lib")))

    assert state.workspace_state == "accepted"


def test_no_working_folder_is_not_a_problem():
    state = roots.evaluate(DeclaredFolders(), (), ())

    assert (state.workspace, state.workspace_state, state.workspace_reason) == (None, "unset", "")


def test_a_working_folder_outside_every_accepted_root_is_refused(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "other").mkdir()

    state = workspace_state(tmp_path, str(tmp_path / "other" / ".work"), DeclaredRoot("lib", str(tmp_path / "lib")))

    assert state.workspace is None and state.workspace_state == "refused" and "not inside an accepted root folder" in state.workspace_reason


def test_a_working_folder_inside_a_root_the_gateway_refused_is_refused(tmp_path):
    (tmp_path / "lib").mkdir()

    state = workspace_state(tmp_path, str(tmp_path / "lib" / ".work"), DeclaredRoot("lib", str(tmp_path / "lib")), mounts=table())

    assert state.workspace_state == "refused" and state.workspace_reason == "no root folder is accepted"


def test_a_link_on_the_way_to_the_working_folder_that_leaves_the_root_is_refused(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "lib" / "go").symlink_to(tmp_path / "outside")

    state = workspace_state(tmp_path, str(tmp_path / "lib" / "go" / ".work"), DeclaredRoot("lib", str(tmp_path / "lib")))

    assert state.workspace_state == "refused" and "leads out of the root folder" in state.workspace_reason


def test_a_working_folder_that_is_a_file_is_refused(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "file").write_text("x")

    state = workspace_state(tmp_path, str(tmp_path / "lib" / "file" / "work"), DeclaredRoot("lib", str(tmp_path / "lib")))

    assert state.workspace_state == "refused" and "is not a directory" in state.workspace_reason


@pytest.mark.parametrize("path", ["relative/work", "/lib/../x", "/"])
def test_a_working_folder_that_is_not_an_absolute_normalized_path_is_refused(tmp_path, path):
    (tmp_path / "lib").mkdir()

    state = workspace_state(tmp_path, path, DeclaredRoot("lib", str(tmp_path / "lib")))

    assert state.workspace_state == "refused"


# ------------------------------------------------------------------
# inspect
# ------------------------------------------------------------------


def test_inspect_of_a_folder_on_a_volume(tmp_path):
    (tmp_path / "lib" / "sub").mkdir(parents=True)

    found = roots.inspect(str(tmp_path / "lib" / "sub"), table(tmp_path / "lib"), ())

    assert found["exists"] and found["directory"] and found["writable"] and found["mounted"] and found["reason"] == ""
    assert found["device"] == "8:0" and found["mount_point"] == os.path.realpath(str(tmp_path / "lib")) and found["fs_type"] == "ext4"
    assert found["real_path"] == os.path.realpath(str(tmp_path / "lib" / "sub")) and found["free_bytes"] > 0


def test_inspect_says_why_a_folder_could_not_be_a_root(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "vol").mkdir()
    (tmp_path / "vol" / "file").write_text("x")
    mounts = table(tmp_path / "vol")

    assert roots.inspect(str(tmp_path / "plain"), mounts, ())["mounted"] is False
    assert "container's own file system" in roots.inspect(str(tmp_path / "plain"), mounts, ())["reason"]
    missing = roots.inspect(str(tmp_path / "vol" / "missing"), mounts, ())
    assert missing["exists"] is False and "cannot be read" in missing["reason"]
    assert roots.inspect(str(tmp_path / "vol" / "file"), mounts, ())["reason"] == "is not a directory"
    assert "overlaps the gateway's own folder" in roots.inspect(str(tmp_path / "vol"), mounts, (str(tmp_path / "vol"),))["reason"]


def test_inspect_of_a_link_reports_what_it_resolves_to(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "vol").mkdir()
    (tmp_path / "vol" / "alias").symlink_to(tmp_path / "plain")

    found = roots.inspect(str(tmp_path / "vol" / "alias"), table(tmp_path / "vol"), ())

    assert found["real_path"] == os.path.realpath(str(tmp_path / "plain")) and found["mounted"] is False


@pytest.mark.parametrize("path", [None, "", "x", "/a/..", "/a//b", "/a/", 3, "a\0b"])
def test_inspect_needs_an_absolute_normalized_path(path):
    with pytest.raises(PathRefused):
        roots.inspect(path, (), ())


# ------------------------------------------------------------------
# browse
# ------------------------------------------------------------------


def names(listing):
    return [entry["name"] for entry in listing["directories"]]


def test_browse_without_a_path_lists_the_volumes_only(tmp_path):
    for name in ("a", "b", "state"):
        (tmp_path / name).mkdir()
    mounts = table(tmp_path / "b", tmp_path / "a", tmp_path / "state", fs="ext4") + (
        Mount(70, 1, "0:70", "/", "/proc", "proc"), Mount(71, 1, "0:71", "/", str(tmp_path / "ram"), "tmpfs"),
        Mount(72, 1, "8:9", "/f", str(tmp_path / "a-file"), "ext4"),
    )
    (tmp_path / "a-file").write_text("x")

    listing = roots.browse(None, mounts, (str(tmp_path / "state"),))

    assert listing["path"] is None and listing["parent"] is None
    assert [d["path"] for d in listing["directories"]] == [os.path.realpath(str(tmp_path / "a")), os.path.realpath(str(tmp_path / "b"))]
    assert all(d["fs_type"] == "ext4" and d["device"].startswith("8:") for d in listing["directories"])
    assert roots.browse("", mounts, ()) ["path"] is None


def test_browse_lists_subdirectories_sorted_and_never_files(tmp_path):
    vol = tmp_path / "vol"
    for name in ("beta", "Alpha", ".hidden"):
        (vol / name).mkdir(parents=True)
    (vol / "song.flac").write_text("x")

    listing = roots.browse(str(vol), table(vol), ())

    assert names(listing) == [".hidden", "Alpha", "beta"]
    assert listing["parent"] is None and listing["truncated"] is False
    assert listing["volume"]["mount_point"] == os.path.realpath(str(vol))
    assert listing["directories"][1]["path"] == str(vol / "Alpha") and listing["directories"][1]["link"] is False


def test_browse_gives_the_parent_of_a_folder_inside_a_volume(tmp_path):
    (tmp_path / "vol" / "a" / "b").mkdir(parents=True)

    listing = roots.browse(str(tmp_path / "vol" / "a"), table(tmp_path / "vol"), ())

    assert names(listing) == ["b"] and listing["parent"] == str(tmp_path / "vol")


def test_browse_lists_a_link_that_stays_in_the_volume_and_hides_one_that_leads_out(tmp_path):
    vol = tmp_path / "vol"
    (vol / "real").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (vol / "inside-link").symlink_to(vol / "real")
    (vol / "out-link").symlink_to(tmp_path / "outside")
    (vol / "dangling").symlink_to(tmp_path / "nowhere")
    (vol / "file-link").symlink_to(vol / "real" / ".." / "real")  # a directory, by another way
    (vol / "to-file").symlink_to(__file__)

    listing = roots.browse(str(vol), table(vol), ())

    assert names(listing) == ["file-link", "inside-link", "real"]
    assert {e["name"]: e["link"] for e in listing["directories"]} == {"file-link": True, "inside-link": True, "real": False}


def test_browse_refuses_a_path_that_leads_out_of_its_volume_through_a_link(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    (tmp_path / "outside").mkdir()
    (vol / "out-link").symlink_to(tmp_path / "outside")

    with pytest.raises(PathRefused, match="leads out of the volume"):
        roots.browse(str(vol / "out-link"), table(vol), ())


def test_browse_refuses_what_is_not_in_a_mounted_volume_the_gateways_own_folders_and_files(tmp_path):
    vol = tmp_path / "vol"
    (vol / "state").mkdir(parents=True)
    (vol / "file").write_text("x")
    mounts = table(vol)

    with pytest.raises(PathRefused, match="not a mounted volume"):
        roots.browse(str(tmp_path), mounts, ())
    with pytest.raises(PathRefused, match="own folder"):
        roots.browse(str(vol / "state"), mounts, (str(vol / "state"),))
    with pytest.raises(PathRefused, match="is not a directory"):
        roots.browse(str(vol / "file"), mounts, ())
    with pytest.raises(PathRefused, match="cannot be read"):
        roots.browse(str(vol / "missing"), mounts, ())
    with pytest.raises(PathRefused):
        roots.browse("relative", mounts, ())


def test_browse_does_not_list_the_gateways_own_folder_inside_a_volume(tmp_path):
    vol = tmp_path / "vol"
    (vol / "state").mkdir(parents=True)
    (vol / "music").mkdir()

    listing = roots.browse(str(vol), table(vol), (str(vol / "state"),))

    assert names(listing) == ["music"]


def test_browse_stops_at_a_limit_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(roots, "MAX_BROWSE_ENTRIES", 3)
    vol = tmp_path / "vol"
    for index in range(5):
        (vol / f"d{index}").mkdir(parents=True)

    listing = roots.browse(str(vol), table(vol), ())

    assert len(listing["directories"]) == 3 and listing["truncated"] is True


def test_the_explicit_check_creates_and_removes_a_hidden_file(tmp_path):
    (tmp_path / "vol").mkdir()

    plain = roots.inspect(str(tmp_path / "vol"), table(tmp_path / "vol"), ())
    checked = roots.inspect(str(tmp_path / "vol"), table(tmp_path / "vol"), (), check_write=True)

    assert "write_test" not in plain, "inspect alone writes nothing"
    assert checked["write_test"] == {"ok": True, "error": ""} and checked["writable"] is True
    assert list((tmp_path / "vol").iterdir()) == [], "the probe file is gone"


def test_the_explicit_check_reports_a_folder_it_cannot_write(tmp_path):
    vol = tmp_path / "vol"
    vol.mkdir()
    vol.chmod(0o555)
    try:
        if os.access(vol, os.W_OK):
            pytest.skip("running as a user that ignores permissions (root)")
        checked = roots.inspect(str(vol), table(vol), (), check_write=True)
    finally:
        vol.chmod(0o755)

    assert checked["write_test"]["ok"] is False and checked["writable"] is False
