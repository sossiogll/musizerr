"""
The version of a recording is part of its identity (``core.matching``):
a remix, a live, a mix or any other version tail never matches another
version of the same song, a duration beyond the tolerance rejects, and the
tracks of two editions of an album pair one to one.

The ``feat.`` credits and the edition markers (remaster, year, explicit...)
are NOT a version: "Song (feat. X)" and "Song - 2011 Remaster" are "Song".
"""
from __future__ import annotations

from hypothesis import assume, given, settings, strategies as st

from core.matching import (
    duration_tolerance_ms,
    match_track,
    pair_album_tracks,
    title_identity_key,
    version_identity,
)
from musizeer_sdk.models import Track

from tests.conftest import make_track

_WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=8)
_BASE_TITLES = st.lists(_WORDS, min_size=1, max_size=4).map(" ".join).filter(lambda t: version_identity(t) == frozenset())
_KINDS = st.sampled_from(["Remix", "Live", "Mix", "Radio Edit", "Acoustic", "Instrumental", "Extended Mix", "Version"])
_CREDITS = st.lists(_WORDS, min_size=1, max_size=3).map(" ".join).filter(
    lambda c: c.split()[0].lower() not in {"feat", "ft", "featuring", "with", "prod"})
_TAILS = st.tuples(_CREDITS, _KINDS).map(lambda p: f"{p[0]} {p[1]}").filter(lambda v: version_identity(f"x ({v})"))
_WRAPS = st.sampled_from(["{t} ({v})", "{t} - {v}", "{t} [{v}]"])


def _track(title: str, **kwargs) -> Track:
    return make_track(title, "Jovanotti", "E si fa bello per te (i remix)", year=2023, **kwargs)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(base=_BASE_TITLES, first=_TAILS, second=_TAILS, wrap_a=_WRAPS, wrap_b=_WRAPS)
def test_two_different_versions_of_a_title_never_match(base, first, second, wrap_a, wrap_b):
    if title_identity_key(f"x ({first})") == title_identity_key(f"x ({second})"):
        return  # the same version, spelled the same way
    source = _track(wrap_a.format(t=base, v=first))
    candidate = _track(wrap_b.format(t=base, v=second))

    assert not match_track(source, candidate).matched


@settings(max_examples=300, deadline=None)
@given(base=_BASE_TITLES, tail=_TAILS, wrap=_WRAPS)
def test_a_version_never_matches_the_plain_title(base, tail, wrap):
    assert not match_track(_track(wrap.format(t=base, v=tail)), _track(base)).matched


@settings(max_examples=300, deadline=None)
@given(base=_BASE_TITLES, tail=_TAILS, wrap_a=_WRAPS, wrap_b=_WRAPS)
def test_the_same_version_matches_whatever_the_punctuation(base, tail, wrap_a, wrap_b):
    assert match_track(_track(wrap_a.format(t=base, v=tail)), _track(wrap_b.format(t=base, v=tail))).matched


@settings(max_examples=300, deadline=None)
@given(duration=st.integers(min_value=30_000, max_value=1_200_000), extra=st.integers(min_value=1, max_value=600_000))
def test_durations_beyond_the_tolerance_never_match(duration, extra):
    other = duration + extra
    assume(extra > duration_tolerance_ms(duration, other))
    result = match_track(_track("Song", duration_ms=duration), _track("Song", duration_ms=other))

    assert not result.matched
    assert result.reason == "duration_mismatch"


def _album(titles_durations, provider):
    return [
        make_track(title, "Jovanotti", "E si fa bello per te (i remix)", duration_ms=duration, provider=provider,
                   provider_id=f"{provider}-{index}")
        for index, (title, duration) in enumerate(titles_durations)
    ]


@settings(max_examples=200, deadline=None)
@given(
    base=_BASE_TITLES,
    tails=st.lists(st.one_of(st.none(), _TAILS), min_size=1, max_size=8),
    durations=st.lists(st.integers(min_value=120_000, max_value=400_000), min_size=8, max_size=8),
    order=st.randoms(use_true_random=False),
)
def test_in_album_pairing_is_one_to_one(base, tails, durations, order):
    source_spec = [(base if tail is None else f"{base} ({tail})", durations[i]) for i, tail in enumerate(tails)]
    candidate_spec = list(source_spec) + [(f"{base} (Bonus Mix)", 199_000)]
    order.shuffle(candidate_spec)
    sources, candidates = _album(source_spec, "a"), _album(candidate_spec, "b")

    pairs = pair_album_tracks(sources, candidates)

    assert len(set(pairs.values())) == len(pairs)
    for s, c in pairs.items():
        assert title_identity_key(sources[s].title) == title_identity_key(candidates[c].title)


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def test_the_remixes_of_one_album_are_ten_different_tracks():
    paren_style = [
        "E si fa bello per te (Remastered 2023)", "E si fa bello per te (Federico Scavo Remix)",
        "E si fa bello per te (Soul Speech Remix)", "E si fa bello per te (Rivaz & Botteghi Remix)",
    ]
    dash_style = [
        "E si fa bello per te - Remastered 2023", "E si fa bello per te - Federico Scavo Remix",
        "E si fa bello per te - Soul Speech Remix", "E si fa bello per te - Rivaz & Botteghi Remix",
    ]
    for i, left in enumerate(paren_style):
        for j, right in enumerate(dash_style):
            assert match_track(_track(left), _track(right)).matched == (i == j), (left, right)


def test_feat_and_edition_markers_are_not_a_version():
    assert title_identity_key("Mil pasos (feat. Antoine Essertier)") == title_identity_key("Mil pasos")
    assert title_identity_key("Song - 2011 Remaster") == title_identity_key("Song (Remastered)")
    assert title_identity_key("Song (Live) - Remastered 2011") == title_identity_key("Song - Live")
    assert title_identity_key("Born This Way (Country Road version)") != title_identity_key("Born This Way")


def test_a_one_year_gap_is_tolerated_when_album_and_duration_agree():
    liked = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", year=2008, duration_ms=246_000)
    library = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", year=2007, duration_ms=246_500)

    assert match_track(liked, library).matched


def test_a_one_year_gap_without_a_duration_still_rejects():
    liked = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", year=2008, duration_ms=0)
    library = make_track("Mil pasos", "Soha", "D'ici et d'ailleurs", year=2007, duration_ms=246_000)

    assert match_track(liked, library).reason == "year_mismatch"


def test_a_larger_year_gap_rejects():
    liked = make_track("Song", "Band", "Record", year=2010, duration_ms=200_000)
    library = make_track("Song", "Band", "Record", year=2008, duration_ms=200_000)

    assert match_track(liked, library).reason == "year_mismatch"


def test_in_album_pairing_prefers_the_track_number_then_the_duration():
    sources = _album([("Intro", 60_000), ("Intro", 90_000)], "a")
    candidates = _album([("Intro", 90_500), ("Intro", 60_500)], "b")
    for position, track in enumerate(sources, start=1):
        track.track_number = position
    candidates[0].track_number, candidates[1].track_number = 2, 1

    assert pair_album_tracks(sources, candidates) == {0: 1, 1: 0}


def test_no_title_only_fallback_when_several_tracks_share_the_base_title():
    # Two same-titled tracks with no album, year or duration to confirm them.
    sources = [make_track("Song", "Band", "", duration_ms=0, provider="a", provider_id="a1"),
               make_track("Song", "Band", "", duration_ms=0, provider="a", provider_id="a2")]
    candidates = [make_track("Song", "Band", "", duration_ms=0, provider="b", provider_id="b1")]

    assert pair_album_tracks(sources, candidates) == {}


def test_a_unique_title_pairs_inside_the_album_even_without_confirmations():
    # The album is already matched: a title that appears once on each side
    # pairs even when the rows carry no album, year or duration.
    sources = [make_track("Song (feat. Guest)", "Band", "", duration_ms=0, provider="a", provider_id="a1"),
               make_track("Other", "Band", "", duration_ms=0, provider="a", provider_id="a2")]
    candidates = [make_track("Other", "Band feat. X", "", duration_ms=0, provider="b", provider_id="b2"),
                  make_track("Song", "Band", "", duration_ms=0, provider="b", provider_id="b1")]

    assert pair_album_tracks(sources, candidates) == {0: 1, 1: 0}
