"""
Pairing the tracks of an album already matched on another provider
(``core.matching.pair_album_tracks``, used by ``services.enrichment``).

It replaces the old title-only fallback (``_match_track_by_base_title``),
which took the FIRST track with the same base title: on an album of remixes
of one song every track took the id of the first one, and all of them
resolved to the one library record of the remaster. Now the pairing is one
to one, the version is part of the title identity, and a title alone pairs
only when it appears once on each side.
"""

from __future__ import annotations

from core.matching import base_title_key, match_track, pair_album_tracks

from tests.conftest import make_track


def _album(*titles, provider="b", artist="Artist", album="", duration_ms=0):
    return [make_track(t, artist, album, duration_ms=duration_ms, provider=provider, provider_id=f"{provider}{i}")
            for i, t in enumerate(titles)]


def test_match_track_absorbs_the_feat_suffix_case():
    src = make_track("Song (feat. Guest)", "Artist", album="Album")
    cand = make_track("Song", "Artist", album="Album")
    assert match_track(src, cand).matched


def test_a_unique_title_pairs_without_confirmations():
    # Album tracks often carry no year/duration/album: match_track has
    # nothing to confirm with, the album context stands in for it.
    [src], [cand] = _album("Song", provider="a"), _album("Song")
    assert not match_track(src, cand).matched
    assert pair_album_tracks([src], [cand]) == {0: 0}


def test_a_unique_title_pairs_with_differently_credited_artists():
    # Inside the matched album the credits may differ (a featured artist on
    # one side only); outside of it match_track still rejects.
    [src], [cand] = _album("Song", provider="a", artist="Artist A"), _album("Song", artist="Different Artist")
    assert pair_album_tracks([src], [cand]) == {0: 0}
    assert not match_track(src, cand).matched


def test_no_pair_on_a_different_title():
    assert pair_album_tracks(_album("Song A", provider="a"), _album("Totally Other")) == {}


def test_an_edition_marker_is_not_a_version():
    assert pair_album_tracks(_album("Song (Remastered)", provider="a"), _album("Different", "Song")) == {0: 1}


def test_two_remixes_of_one_song_never_pair():
    sources = _album("Song (Federico Scavo Remix)", provider="a")
    assert pair_album_tracks(sources, _album("Song (Soul Speech Remix)", "Song - Remastered 2023")) == {}


def test_every_remix_pairs_with_its_own_version():
    sources = _album("Song (Remastered 2023)", "Song (Federico Scavo Remix)", "Song (Soul Speech Remix)", provider="a")
    candidates = _album("Song - Soul Speech Remix", "Song - Remastered 2023", "Song - Federico Scavo Remix")
    assert pair_album_tracks(sources, candidates) == {0: 1, 1: 2, 2: 0}


def test_base_title_reuses_core_normalization():
    assert base_title_key("KoЯn - 2012 Remaster") == "korn"
