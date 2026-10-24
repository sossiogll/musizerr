"""The pure rules of root folders and folder values (``core/folders.py``, ADR-0008): what a root's path may be, the shape
of a value, the folder it resolves to (``..``, a link that leaves the root, nested roots) and the conversion helpers."""
from __future__ import annotations

import pytest

from core import folders as rules


# ------------------------------------------------------------------
# The path of a root
# ------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("/library", "/library"), ("  /library  ", "/library"), ("/library/", "/library"), ("/media/Music Library", "/media/Music Library"),
    ("/a/b/", "/a/b"), ("/downloads/.musizeer-work", "/downloads/.musizeer-work"),
])
def test_a_typed_root_path_is_cleaned_of_blanks_and_a_trailing_slash(raw, expected):
    assert rules.normalize_root_path(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "/", "//", "relative/path", "./x", "/a/../b", "/a/./b", "/a//b", "/..", "/a/..", "x" * 5000, "/a\0b", None, 5, ["/a"],
])
def test_a_root_path_that_is_not_absolute_and_clean_is_refused_never_silently_collapsed(raw):
    with pytest.raises(rules.FolderError) as refused:
        rules.normalize_root_path(raw)

    assert refused.value.code == rules.ROOT_PATH_INVALID


def test_the_problem_of_a_root_path_says_why():
    assert rules.root_path_problem("/ok") is None
    assert "absolute" in rules.root_path_problem("ok")
    assert "'..'" in rules.root_path_problem("/a/../b")
    assert "'/'" in rules.root_path_problem("/")


@pytest.mark.parametrize("path, label", [("/library", "Library"), ("/media/music", "Music"), ("/downloads", "Downloads"), ("/x", "X"), ("/a/.hidden", "Hidden")])
def test_a_root_without_a_label_gets_its_last_component_capitalised(path, label):
    assert rules.label_for(path) == label


def test_a_label_is_trimmed_and_never_empty_or_multiline():
    assert rules.clean_label("  Music  ") == "Music"
    assert rules.clean_label("", "Fallback") == "Fallback"
    for bad in ("", "   ", "a\nb", "x" * 81):
        with pytest.raises(rules.FolderError) as refused:
            rules.clean_label(bad)
        assert refused.value.code == rules.ROOT_LABEL_INVALID


# ------------------------------------------------------------------
# The value of a folder setting
# ------------------------------------------------------------------


def test_a_value_is_a_root_id_and_a_relative_path_and_nothing_else():
    value = rules.parse_value({"root_id": "r1", "path": "Music/Rock"})

    assert value == rules.FolderValue("r1", "Music/Rock") and value.stored() == {"root_id": "r1", "path": "Music/Rock"}
    assert rules.parse_value({"root_id": "r1", "path": ""}).path == "", "the empty path is the root itself"


@pytest.mark.parametrize("raw", [
    "/library", "", None, [], {"root_id": "r1"}, {"path": "x"}, {"root_id": "r1", "path": "x", "extra": 1}, {"root_id": 5, "path": "x"},
    {"root_id": "", "path": "x"}, {"root_id": "  ", "path": "x"}, {"root_id": "r" * 65, "path": "x"}, {"root_id": "r1", "path": None},
])
def test_what_is_not_a_root_and_a_path_is_not_a_folder_value(raw):
    with pytest.raises(rules.FolderError) as refused:
        rules.parse_value(raw)

    assert refused.value.code in (rules.VALUE_INVALID, rules.PATH_INVALID)


@pytest.mark.parametrize("path", ["/abs", "../x", "a/../b", "a//b", "a/", "./a", "a/./b", "a\0", "x" * 4097])
def test_a_path_inside_a_root_is_relative_and_normalized(path):
    with pytest.raises(rules.FolderError) as refused:
        rules.parse_value({"root_id": "r1", "path": path})

    assert refused.value.code == rules.PATH_INVALID


def test_an_unset_value_is_none_an_empty_string_or_an_empty_object():
    assert all(rules.is_unset(raw) for raw in (None, "", {}))
    assert not rules.is_unset({"root_id": "r", "path": ""}) and not rules.is_unset("/library")


def test_a_reader_treats_an_old_text_or_a_broken_value_as_not_set():
    assert rules.value_or_none("/library") is None, "what an older version stored is not a folder"
    assert rules.value_or_none({"root_id": "r"}) is None and rules.value_or_none(None) is None
    assert rules.value_or_none({"root_id": "r", "path": "x"}) == rules.FolderValue("r", "x")


# ------------------------------------------------------------------
# Resolution
# ------------------------------------------------------------------


def test_a_folder_is_the_root_joined_with_the_relative_path():
    assert rules.resolve("/library", "") == "/library"
    assert rules.resolve("/library", "Music") == "/library/Music"
    assert rules.resolve("/downloads", ".musizeer-work") == "/downloads/.musizeer-work"
    assert rules.resolve("/media", "Musica-staging/.musizeer-work") == "/media/Musica-staging/.musizeer-work"


@pytest.mark.parametrize("path", ["..", "../etc", "a/../..", "a/../../b", "/etc", "a//b", "./a"])
def test_a_path_that_would_leave_the_root_is_refused_before_anything_is_joined(path):
    with pytest.raises(rules.FolderError) as refused:
        rules.resolve("/library", path)

    assert refused.value.code == rules.PATH_INVALID


def fake_real(table: dict[str, str]):
    """The real paths of an existing set of folders; everything else does not exist."""
    return lambda path: table.get(path)


def test_a_folder_that_exists_must_stay_inside_the_real_path_of_the_root():
    real = fake_real({"/library": "/mnt/disk/Musica", "/library/Music": "/mnt/disk/Musica/Music"})

    assert rules.resolve("/library", "Music", real) == "/library/Music"


def test_a_link_that_leaves_the_root_is_refused():
    real = fake_real({"/library": "/mnt/disk/Musica", "/library/escape": "/etc"})

    with pytest.raises(rules.FolderError) as refused:
        rules.resolve("/library", "escape", real)

    assert refused.value.code == rules.LEAVES_ROOT


def test_a_folder_that_does_not_exist_yet_is_judged_by_the_nearest_one_that_does():
    real = fake_real({"/library": "/mnt/disk/Musica", "/library/escape": "/etc"})

    with pytest.raises(rules.FolderError) as refused:
        rules.resolve("/library", "escape/new/deeper", real)
    assert refused.value.code == rules.LEAVES_ROOT
    assert rules.resolve("/library", "fresh/folder", real) == "/library/fresh/folder", "nothing on the way is a link"


def test_a_root_that_the_host_does_not_see_is_not_compared_with_anything():
    """The library is mounted in the gateway only: the host has nothing to compare, and the gateway judges."""
    assert rules.resolve("/library", "Music", fake_real({})) == "/library/Music"


def test_the_link_to_a_root_inside_the_same_volume_is_fine():
    real = fake_real({"/library": "/mnt/disk/Musica", "/library/alias": "/mnt/disk/Musica/Real"})

    assert rules.resolve("/library", "alias", real) == "/library/alias"


# ------------------------------------------------------------------
# Nested roots, and the root that holds a path
# ------------------------------------------------------------------


def test_the_deepest_root_that_contains_a_path_is_the_one_that_holds_it():
    roots = [("media", "/media"), ("music", "/media/Musica"), ("dl", "/downloads")]

    assert rules.containing(roots, "/media/Musica/Rock") == ("music", "Rock")
    assert rules.containing(roots, "/media/Musica") == ("music", "")
    assert rules.containing(roots, "/media/Video") == ("media", "Video")
    assert rules.containing(roots, "/downloads/.musizeer-work") == ("dl", ".musizeer-work")
    assert rules.containing(roots, "/elsewhere") is None


def test_a_longer_name_is_not_inside_a_root():
    assert rules.containing([("d", "/downloads")], "/downloads-evil/x") is None
    assert not rules.inside("/downloads-evil", "/downloads") and rules.inside("/downloads/a", "/downloads")


def test_a_folder_in_a_nested_root_resolves_the_same_by_either_root():
    assert rules.resolve("/media", "Musica/Rock") == rules.resolve("/media/Musica", "Rock")


# ------------------------------------------------------------------
# The conversion of the old working folder
# ------------------------------------------------------------------

OLD, NEW = ".old-hidden-name", ".musizeer-work"


@pytest.mark.parametrize("value, expected", [
    ("/downloads/" + OLD, ("/downloads", NEW)),
    ("/downloads/" + OLD + "/", ("/downloads", NEW)),
    ("/media/staging/" + OLD, ("/media/staging", NEW)),
    ("/data/work", ("/data", "work")),
    ("/library/.work", ("/library", ".work")),
])
def test_the_old_working_folder_keeps_its_parent_and_the_old_hidden_name_becomes_the_new_one(value, expected):
    assert rules.legacy_working_folder(value, OLD, NEW) == expected


@pytest.mark.parametrize("value", ["/downloads", "/", "relative/work", "/a/../b", ""])
def test_a_working_folder_that_no_root_could_hold_is_not_converted(value):
    with pytest.raises(rules.FolderError):
        rules.legacy_working_folder(value, OLD, NEW)
