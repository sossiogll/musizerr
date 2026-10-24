"""
Every hyphen-like character reads the same in a title (``musizeer_sdk.text``):
"Backup 1987-2012" and "Backup 1987–2012" are one album, "Song - Live" and
"Song – Live" carry the same version, but a subtitle still makes a different
release ("Backup 1987-2012" is not "Backup 1987–2012: Il best").
"""
from __future__ import annotations

import pytest

from core.matching import (
    base_title_key,
    match_album,
    match_track,
    release_title_key,
    title_identity_key,
    version_identity,
)

from tests.conftest import make_album, make_track

DASHES = ["-", "‐", "‑", "‒", "–", "—", "―", "−"]


@pytest.mark.parametrize("dash", DASHES)
def test_the_same_album_with_any_dash_in_its_title_matches(dash):
    a = make_album(f"Backup 1987-2012", "Some Artist", provider="alpha", year=2012)
    b = make_album(f"Backup 1987{dash}2012", "Some Artist", provider="beta", year=2012)

    assert match_album(a, b).matched
    assert release_title_key(a.title) == release_title_key(b.title)


@pytest.mark.parametrize("dash", DASHES)
def test_a_subtitle_still_makes_another_release(dash):
    plain = make_album("Backup 1987-2012", "Some Artist", provider="alpha", year=2012)
    subtitled = make_album(f"Backup 1987{dash}2012: Il best", "Some Artist", provider="beta", year=2012)

    result = match_album(plain, subtitled)

    assert not result.matched and result.reason == "title_mismatch"
    assert release_title_key(plain.title) != release_title_key(subtitled.title)


@pytest.mark.parametrize("dash", DASHES)
def test_a_version_tail_after_any_spaced_dash_is_the_same_version(dash):
    hyphen = "Song - Federico Scavo Remix"
    other = f"Song {dash} Federico Scavo Remix"

    assert version_identity(other) == version_identity(hyphen) == frozenset({"federico scavo remix"})
    assert title_identity_key(other) == title_identity_key(hyphen)
    assert base_title_key(other) == base_title_key("Song")


@pytest.mark.parametrize("dash", DASHES)
def test_the_live_version_is_not_the_studio_song_whatever_the_dash(dash):
    live = make_track(f"Song {dash} Live", "Artist", "Live Album", provider="alpha", isrc=None)
    studio = make_track("Song", "Artist", "Album", provider="beta", isrc=None)
    live_hyphen = make_track("Song - Live", "Artist", "Live Album", provider="beta", isrc=None)

    assert not match_track(live, studio).matched
    assert match_track(live, live_hyphen).matched
