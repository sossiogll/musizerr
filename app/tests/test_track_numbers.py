"""
Numero di traccia e di disco: letti dai provider, conservati fino al worker
e usati dal template del nome file. Con il numero sconosciuto il nome non
contiene "00": il segnaposto sparisce insieme al suo separatore.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from musizeer_sdk.models import Track
from core.serialization import deserialize_track, serialize_track
from services.library_paths import library_path

from tests.conftest import make_track


def _cfg(fmt="{artist}/{album}/{track_number:02d}. {title}"):
    return SimpleNamespace(track_format=fmt)


def test_the_template_uses_the_real_number():
    track = make_track(title="Larsen", artist="Caparezza", album="Prisoner 709")
    track.track_number = 7

    assert library_path(track, _cfg().track_format, ".flac") == "Caparezza/Prisoner 709/07. Larsen.flac"


@pytest.mark.parametrize("fmt, expected", [
    ("{artist}/{album}/{track_number:02d}. {title}", "Caparezza/Prisoner 709/Larsen.mp3"),
    ("{artist}/{album}/{track_number} - {title}", "Caparezza/Prisoner 709/Larsen.mp3"),
    ("{artist}/{album}/{title}", "Caparezza/Prisoner 709/Larsen.mp3"),
])
def test_an_unknown_number_never_becomes_00(fmt, expected):
    track = make_track(title="Larsen", artist="Caparezza", album="Prisoner 709")

    path = library_path(track, fmt, ".mp3")

    assert path == expected
    assert "00" not in path


def test_numbers_survive_the_trip_to_the_worker():
    track = make_track(year=2017)
    track.track_number, track.disc_number = 3, 2

    back = Track.from_dict(track.to_dict())

    assert (back.track_number, back.disc_number, back.year) == (3, 2, 2017)


def test_numbers_survive_serialization():
    track = make_track()
    track.track_number, track.disc_number = 5, 1

    back = deserialize_track(serialize_track(track))

    assert (back.track_number, back.disc_number) == (5, 1)

