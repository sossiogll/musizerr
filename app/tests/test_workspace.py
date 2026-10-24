"""Cartelle di lavoro dell'host (ADR-0003, services/workspace.py)."""
from __future__ import annotations

import os

import pytest

from musizeer_sdk.interfaces import StorageStreamResult
from services.workspace import WorkspaceError, open_workspace, remove_stale_workspaces


def test_a_workspace_is_empty_and_removed_on_exit_even_after_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        with open_workspace(tmp_path, "demo") as ws:
            assert list(ws.path.iterdir()) == []
            (ws.path / "song.flac").write_bytes(b"x")
            created = ws.path
            raise RuntimeError("boom")
    assert not created.exists()


def test_each_call_gets_its_own_workspace(tmp_path):
    with open_workspace(tmp_path, "demo") as a, open_workspace(tmp_path, "demo") as b:
        assert a.path != b.path


def test_a_name_returned_by_a_provider_resolves_inside_the_workspace(tmp_path):
    with open_workspace(tmp_path, "demo") as ws:
        (ws.path / "sub").mkdir()
        (ws.path / "sub" / "song.flac").write_bytes(b"x")

        f = ws.file("sub/song.flac")

        assert f.name == "sub/song.flac"
        assert f.path == str(ws.path / "sub" / "song.flac")


@pytest.mark.parametrize("name", ["../escape.flac", "/etc/passwd", ".", "missing.flac"])
def test_names_outside_the_workspace_or_missing_are_refused(tmp_path, name):
    (tmp_path / "escape.flac").write_bytes(b"x")
    with open_workspace(tmp_path / "root", "demo") as ws:
        with pytest.raises(WorkspaceError):
            ws.file(name)


def test_a_symlink_pointing_outside_is_refused(tmp_path):
    outside = tmp_path / "secret"
    outside.write_bytes(b"x")
    with open_workspace(tmp_path / "root", "demo") as ws:
        os.symlink(outside, ws.path / "link.flac")
        with pytest.raises(WorkspaceError):
            ws.file("link.flac")


def test_only_workspaces_older_than_the_max_age_are_removed(tmp_path):
    old = tmp_path / "storage" / "old"
    fresh = tmp_path / "storage" / "fresh"
    other = tmp_path / "sync" / "old"
    for directory in (old, fresh, other):
        directory.mkdir(parents=True)
    os.utime(old, (1000, 1000))
    os.utime(other, (1000, 1000))
    os.utime(fresh, (5000, 5000))

    removed = remove_stale_workspaces(tmp_path, max_age_seconds=1000, now=5500)

    assert removed == 2
    assert not old.exists() and not other.exists() and fresh.exists()
    assert (tmp_path / "storage").is_dir() and (tmp_path / "sync").is_dir(), "the folder of a provider is the gateway's mount: it stays"


def test_the_folder_of_the_held_uploads_is_never_swept(tmp_path):
    held = tmp_path / "held-uploads" / "track"
    held.mkdir(parents=True)
    os.utime(held, (1000, 1000))

    assert remove_stale_workspaces(tmp_path, max_age_seconds=1, now=999_999) == 0
    assert held.exists()


def test_a_missing_root_is_nothing_to_sweep(tmp_path):
    assert remove_stale_workspaces(tmp_path / "nope", max_age_seconds=1) == 0


def test_a_streamed_file_is_read_in_place(tmp_path):
    with open_workspace(tmp_path, "demo") as ws:
        (ws.path / "song.flac").write_bytes(b"audio")

        path = ws.local_file(StorageStreamResult(file="song.flac"))

        assert path == str(ws.path / "song.flac")


def test_streamed_chunks_are_written_into_the_workspace(tmp_path):
    with open_workspace(tmp_path, "demo") as ws:
        path = ws.local_file(StorageStreamResult(chunks=iter([b"au", b"dio"]), filename="song.flac"))

        assert path is not None and path.endswith(".flac")
        assert os.path.dirname(path) == str(ws.path)
        with open(path, "rb") as f:
            assert f.read() == b"audio"


def test_a_streamed_url_has_no_local_file(tmp_path):
    with open_workspace(tmp_path, "demo") as ws:
        assert ws.local_file(StorageStreamResult(url="http://example.test/song.flac")) is None


def test_a_workspace_is_in_the_folder_of_its_provider_and_open_to_its_sandbox(tmp_path):
    from services.workspace import new_workspace

    ws = new_workspace(tmp_path, "storage")

    assert ws.path.parent == tmp_path / "storage"
    assert (ws.path.stat().st_mode & 0o777) == 0o777, "the sandbox runs as another user and must be able to write"
    assert (ws.path.parent.stat().st_mode & 0o777) == 0o711, "crossed by everyone, listed by its owner only"
    assert new_workspace(tmp_path, "storage").path != ws.path


@pytest.mark.parametrize("provider", ["", "../x", "a/b", "Upper", "held-uploads", ".hidden", "x" * 80, "a b"])
def test_only_a_plain_provider_name_can_name_a_folder(tmp_path, provider):
    from services.workspace import new_workspace

    with pytest.raises(WorkspaceError):
        new_workspace(tmp_path, provider)
    assert list(tmp_path.iterdir()) == []


def test_an_operation_that_tries_several_providers_gets_one_workspace_for_each_and_removes_them_together(tmp_path):
    from services.workspace import open_workspaces

    with open_workspaces(tmp_path) as workspaces:
        first = workspaces.for_provider("one")
        assert workspaces.for_provider("one") is first
        second = workspaces.for_provider("two")
        assert workspaces.path_for("two") == str(second.path) and first.path.parent != second.path.parent
        (first.path / "f").write_bytes(b"x")

    assert not first.path.exists() and not second.path.exists()
    assert list(tmp_path.glob("*/*")) == []
