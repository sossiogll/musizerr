"""
Il path in libreria lo decide l'host dai metadati, che vengono da terzi: un
artista ".." o vuoto, un album mancante o un formato che non si applica non
devono mai portare un file fuori dalla libreria né far fallire il download.
"""
from __future__ import annotations

import pytest

from services.library_paths import library_path

from tests.conftest import make_track

DEFAULT = "{artist}/{album}/{track_number:02d}. {title}"


def _path(fmt=DEFAULT, **fields):
    track = make_track(**{"title": "Larsen", "artist": "Caparezza", "album": "Prisoner 709", **fields})
    return library_path(track, fmt, ".mp3")


@pytest.mark.parametrize("fields, expected", [
    ({"artist": ".."}, "Unknown/Prisoner 709/Larsen.mp3"),
    ({"artist": "", "album": ""}, "Unknown/Unknown/Larsen.mp3"),
    ({"album": None}, "Caparezza/Unknown/Larsen.mp3"),
    ({"title": "."}, "Caparezza/Prisoner 709/Unknown.mp3"),
    ({"artist": "../../etc"}, ".._.._etc/Prisoner 709/Larsen.mp3"),
])
def test_metadata_never_leaves_the_library(fields, expected):
    assert _path(**fields) == expected


@pytest.mark.parametrize("fmt, expected", [
    ("/{artist}/{title}", "Caparezza/Larsen.mp3"),
    ("../{artist}//{title}", "_/Caparezza/Larsen.mp3"),
])
def test_the_format_itself_never_leaves_the_library(fmt, expected):
    assert _path(fmt) == expected


@pytest.mark.parametrize("fmt", ["{release_year:04d}/{title}", "{0}/{title}", "{nope}/{title}"])
def test_a_format_that_does_not_apply_falls_back_instead_of_failing(fmt):
    assert _path(fmt, year=None) == "Caparezza/Prisoner 709/Larsen.mp3"
