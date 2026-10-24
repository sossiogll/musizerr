"""``musizeer_sdk.folders``: the one rule of the path inside a root folder (ADR-0008)."""
from __future__ import annotations

import pytest

from musizeer_sdk.folders import MAX_PATH, is_relative_path, relative_path_problem


@pytest.mark.parametrize("path", ["", "a", "a/b", ".hidden", "..x", "a/.b/c", "x y/z", "Artist - Album/01.flac", "a" * MAX_PATH])
def test_a_relative_normalized_path_is_usable(path):
    assert relative_path_problem(path) is None and is_relative_path(path)


@pytest.mark.parametrize("path, why", [
    ("/", "absolute"),
    ("/a", "absolute"),
    (".", "'.' segment"),
    ("..", "'..' segment"),
    ("a/..", "'..' segment"),
    ("../a", "'..' segment"),
    ("a/./b", "'.' segment"),
    ("a//b", "empty segment"),
    ("a/", "empty segment"),
    ("a\0", "NUL"),
    ("a" * (MAX_PATH + 1), "longer than"),
    (None, "not text"),
    (b"a", "not text"),
])
def test_what_is_not_a_path_inside_a_root_says_why(path, why):
    problem = relative_path_problem(path)

    assert problem is not None and why in problem
    assert not is_relative_path(path)
