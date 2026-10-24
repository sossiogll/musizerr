"""
The decoration of a video title ("(Official Video)", "[HD]", "- Lyrics",
"(Video Ufficiale)") is not part of the song and is NOT a version: like the
``feat.`` credits and the edition markers, it never keeps two tracks apart.
A real version (live, remix, acoustic, edit, mix...) stays one, with or without
the decoration around it.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from core.matching import base_title_key, match_track, song_keys, title_identity_key, version_identity
from musizeer_sdk.text import is_video_noise, strip_video_noise

from tests.conftest import make_track
from tests.test_matching_versions import _BASE_TITLES, _TAILS, _WRAPS

NOISE = [
    "Official Video", "Official Music Video", "Official Audio", "Official Lyric Video", "Official Lyrics Video",
    "Lyric Video", "Lyrics", "Lyrics Video", "Visualizer", "Visualiser", "Audio", "HD", "4K", "Official Video HD",
    "HD Official Video", "With Lyrics", "Videoclip Ufficiale", "Video Ufficiale", "Audio Ufficiale", "Testo",
    "Official Visualizer", "Official Music Video 4K", "Clip Officiel",
]
_NOISE = st.sampled_from(NOISE)
_NOISE_WRAPS = st.sampled_from(["{t} ({n})", "{t} [{n}]", "{t} - {n}", "{t} | {n}", "{t} ({n}) [HD]", "{t} – {n}"])


def _song(title: str, **kwargs):
    return make_track(title, "Band", "", duration_ms=200_000, **kwargs)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(base=_BASE_TITLES.filter(lambda t: not is_video_noise(t)), noise=_NOISE, wrap=_NOISE_WRAPS)
def test_video_noise_is_never_part_of_the_identity(base, noise, wrap):
    decorated = wrap.format(t=base, n=noise)

    assert version_identity(decorated) == frozenset()
    assert title_identity_key(decorated) == title_identity_key(base)
    assert base_title_key(decorated) == base_title_key(base)
    assert match_track(_song(decorated), _song(base)).matched
    assert song_keys(_song(decorated)) == song_keys(_song(base))


@settings(max_examples=300, deadline=None)
@given(base=_BASE_TITLES.filter(lambda t: not is_video_noise(t)), tail=_TAILS, noise=_NOISE,
       wrap=_WRAPS, noise_wrap=_NOISE_WRAPS)
def test_a_real_version_stays_a_version_with_the_noise_around_it(base, tail, noise, wrap, noise_wrap):
    version = wrap.format(t=base, v=tail)
    decorated = noise_wrap.format(t=version, n=noise)

    assert version_identity(decorated) == version_identity(version) != frozenset()
    assert not match_track(_song(decorated), _song(base)).matched
    assert match_track(_song(decorated), _song(version)).matched




# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decorated", [
    "Song (Official Video)", "Song (Official Music Video)", "Song (Official Audio)", "Song (Official Lyric Video)",
    "Song (Lyric Video)", "Song (Lyrics)", "Song (Visualizer)", "Song (Audio)", "Song [HD]", "Song [4K]",
    "Song [Official Video]", "Song - Official Video", "Song | Official Video", "Song (Video Ufficiale)",
    "Song (Testo)", "Song Official Video", "Song Official Music Video", "Song (Official Video) [HD]",
    "Song - Official Video | HD", "Song (feat. Guest) (Official Video)",
])
def test_the_decoration_of_a_video_title_names_no_version(decorated):
    assert version_identity(decorated) == frozenset()
    assert match_track(_song(decorated), _song("Song")).matched
    assert base_title_key(decorated) == "song"


def test_a_credit_with_the_noise_is_still_only_a_credit():
    assert match_track(_song("Song (feat. Guest) (Official Video)"), _song("Song (feat. Guest)")).matched
    assert match_track(_song("Song (feat. Guest) (Official Video)"), _song("Song")).matched


@pytest.mark.parametrize("real, expected", [
    ("Song (Live) (Official Video)", {"live"}),
    ("Song (Official Video) - Live", {"live"}),
    ("Song [Official Audio] (Acoustic)", {"acoustic"}),
    ("Song (Federico Scavo Remix) [Official Video]", {"federico scavo remix"}),
    ("Song - Federico Scavo Remix (Official Video)", {"federico scavo remix"}),
    ("Song (Radio Edit) | Official Video", {"radio edit"}),
    ("Song (Live at Wembley) [HD]", {"live at wembley"}),
    ("Song (Official Video) (Extended Mix)", {"extended mix"}),
])
def test_real_versions_survive_the_noise(real, expected):
    assert version_identity(real) == frozenset(expected)
    assert not match_track(_song(real), _song("Song")).matched


def test_the_same_version_with_and_without_the_noise_matches():
    assert match_track(_song("Song (Live) [Official Video]"), _song("Song - Live")).matched
    assert match_track(_song("Song (Remix) (Official Audio)"), _song("Song - Remix")).matched


def test_two_versions_stay_apart_whatever_the_noise():
    assert not match_track(_song("Song (Live) (Official Video)"), _song("Song (Remix) (Official Video)")).matched
    assert not match_track(_song("Song (Live) [HD]"), _song("Song (Acoustic) [HD]")).matched


@pytest.mark.parametrize("text, noise", [
    ("Official Video", True), ("official music video", True), ("HD", True), ("4K", True), ("Lyrics", True),
    ("Video Ufficiale", True), ("Testo", True), ("Visualizer", True),
    # a lone weak word, or anything with a real word, is not noise
    ("Music", False), ("Con", False), ("", False), ("Live", False), ("Live Video", False), ("Remix", False),
    ("Radio Edit", False), ("Official Remix", False), ("feat. Guest", False), ("2011 Remaster", False),
])
def test_what_counts_as_video_noise(text, noise):
    assert is_video_noise(text) is noise


@pytest.mark.parametrize("title, expected", [
    ("Song (Official Video)", "Song"),
    ("Song [HD] (Official Audio)", "Song"),
    ("Song - Official Video | HD", "Song"),
    ("Song Official Music Video", "Song"),
    ("Song (Live) (Official Video)", "Song (Live)"),
    ("Song - Live", "Song - Live"),
    ("Song - Radio Edit", "Song - Radio Edit"),
    ("Song (Official Remix)", "Song (Official Remix)"),
    ("Official Video", "Official Video"),          # nothing else would be left: kept
    ("(Official Video)", "(Official Video)"),
    ("Lyrics", "Lyrics"),                          # a song called Lyrics
    ("Song Lyrics", "Song Lyrics"),                # no punctuation, only the multi-word phrases go
])
def test_strip_video_noise(title, expected):
    assert strip_video_noise(title) == expected


def test_a_song_named_like_the_noise_still_matches_itself():
    assert match_track(_song("Audio"), _song("Audio")).matched
    assert match_track(_song("Video (Official Video)"), _song("Video")).matched


@pytest.mark.parametrize("title, identity, key", [
    ("Last Night (Spencer and Hill remix)", {"spencer and hill remix"}, "last night | spencer and hill remix"),
    ("QWERTY (Live in Tokyo, 2006)", {"live in tokyo"}, "qwerty | live in tokyo"),
    ("abc (The Wild Remix)", {"the wild remix"}, "abc | the wild remix"),
    ("Cantu Siciliano - Video Edit", {"video edit"}, "cantu siciliano | video edit"),
    ("Vamos A La Playa (Movetown Video Edit)", {"movetown video edit"}, "vamos a la playa | movetown video edit"),
    ("Give It To Me - Full Vocal Mix", {"full vocal mix"}, "give it to me | full vocal mix"),
    ("Perfect Paganini (Full Original Mix)", {"full original mix"}, "perfect paganini | full original mix"),
    ("Who You Are - Full English Version", {"full english version"}, "who you are | full english version"),
])
def test_the_words_of_a_real_version_tail_are_never_dropped(title, identity, key):
    """Video noise goes only as a whole group (a bracket, a separator tail, a
    bare phrase), never word by word from inside a real version: these
    identities are the ones the titles always had."""
    assert version_identity(title) == frozenset(identity)
    assert title_identity_key(title) == key
