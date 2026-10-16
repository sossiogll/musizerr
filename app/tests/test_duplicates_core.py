"""
Test puri (nessun DB, nessun provider) per ``core.duplicates``: clustering
dei ``LibraryItem`` e policy del keeper. Golden Rule rispettata: nessun
nome di provider concreto qui.
"""

from __future__ import annotations

from core.duplicates import (
    DuplicateCandidate,
    best_quality_key,
    decide_keeper,
    cluster_library_items,
    normalize_library_path,
    quality_from_library_item,
)

from tests.conftest import make_library_item


# ------------------------------------------------------------------
# normalize_library_path
# ------------------------------------------------------------------

def test_normalize_library_path_strips_leading_slash_and_dot_slash():
    assert normalize_library_path("/Music/Song.flac") == "Music/Song.flac"
    assert normalize_library_path("./Music/Song.flac") == "Music/Song.flac"
    assert normalize_library_path("Music\\Song.flac") == "Music/Song.flac"
    assert normalize_library_path("Music/Song.flac") == "Music/Song.flac"


# ------------------------------------------------------------------
# cluster_library_items
# ------------------------------------------------------------------

def test_cluster_groups_same_title_artist_album_year():
    a = make_library_item("Snow (Hey Oh)", "Red Hot Chili Peppers", "Stadium Arcadium", path="a.mp3", year=2006)
    b = make_library_item("Snow (Hey Oh)", "Red Hot Chili Peppers", "Stadium Arcadium", path="b.ogg", year=2006)

    clusters = cluster_library_items([a, b])

    assert len(clusters) == 1
    assert {i.path for i in clusters[0]} == {"a.mp3", "b.ogg"}


def test_cluster_does_not_group_different_album_or_year():
    # Stessa policy di tutta l'app (core.matching.match_album): album/anno
    # diversi = release diversa, mai un duplicato.
    a = make_library_item("Song", "Artist", "Studio Album", path="a.mp3", year=2010)
    b = make_library_item("Song", "Artist", "Live Album", path="b.mp3", year=2015)

    clusters = cluster_library_items([a, b])

    assert clusters == []


def test_cluster_ignores_singletons():
    a = make_library_item("Unique Song", "Artist", "Album", path="a.mp3")
    other = make_library_item("Another Song", "Other Artist", "Other Album", path="b.mp3")

    clusters = cluster_library_items([a, other])

    assert clusters == []


# ------------------------------------------------------------------
# decide_keeper
# ------------------------------------------------------------------

def test_keeper_prefers_lossless_over_lossy_when_both_deletable():
    lossy = DuplicateCandidate(key="a.mp3", quality=quality_from_library_item(
        make_library_item(suffix="mp3", bitrate=320000, sample_rate=44100)), deletable=True)
    lossless = DuplicateCandidate(key="b.flac", quality=quality_from_library_item(
        make_library_item(suffix="flac", bitrate=1000000, sample_rate=44100)), deletable=True)

    decision = decide_keeper([lossy, lossless], prefer_lossless=True)

    assert decision.status == "proposed"
    assert decision.keeper_key == "b.flac"
    assert decision.reason == "best_quality"


def test_keeper_needs_review_when_any_quality_unknown():
    known = DuplicateCandidate(key="a.flac", quality=quality_from_library_item(
        make_library_item(suffix="flac", bitrate=1000000, sample_rate=44100)), deletable=True)
    unknown = DuplicateCandidate(key="b", quality=quality_from_library_item(
        make_library_item(suffix="")), deletable=True)

    decision = decide_keeper([known, unknown])

    assert decision.status == "needs_review"
    assert decision.keeper_key is None
    assert decision.reason == "unknown_quality"


def test_keeper_needs_review_when_losing_copy_is_not_deletable():
    # Il "vincitore" per qualità implicherebbe cancellare un file di origine
    # esterna/sconosciuta (deletable=False) -> mai automatico, va all'admin.
    winner = DuplicateCandidate(key="a.flac", quality=quality_from_library_item(
        make_library_item(suffix="flac", bitrate=1000000, sample_rate=44100)), deletable=True)
    external_loser = DuplicateCandidate(key="b.mp3", quality=quality_from_library_item(
        make_library_item(suffix="mp3", bitrate=128000, sample_rate=44100)), deletable=False)

    decision = decide_keeper([winner, external_loser])

    assert decision.status == "needs_review"
    assert decision.keeper_key is None
    assert decision.reason == "external_or_unknown_origin_would_be_deleted"


def test_keeper_deterministic_tie_break_by_key():
    a = DuplicateCandidate(key="a.flac", quality=quality_from_library_item(
        make_library_item(suffix="flac", bitrate=1000000, sample_rate=44100)), deletable=True)
    b = DuplicateCandidate(key="b.flac", quality=quality_from_library_item(
        make_library_item(suffix="flac", bitrate=1000000, sample_rate=44100)), deletable=True)

    decision_1 = decide_keeper([a, b])
    decision_2 = decide_keeper([b, a])

    # Stesso esito indipendentemente dall'ordine di input.
    assert decision_1.keeper_key == decision_2.keeper_key == "b.flac"


def test_quality_from_library_item_unknown_when_no_suffix():
    item = make_library_item(suffix="")
    assert quality_from_library_item(item) is None


def test_quality_from_library_item_known_lossless():
    item = make_library_item(suffix="flac", bitrate=900000, sample_rate=44100)
    quality = quality_from_library_item(item)
    assert quality is not None
    assert quality.lossless is True
    assert quality.bitrate == 900000


# ------------------------------------------------------------------
# best_quality_key — the hint for a group the admin decides by hand
# ------------------------------------------------------------------

def _quality(key: str, *, lossless=False, bitrate=0, known=True, deletable=False) -> DuplicateCandidate:
    from core.audio_quality import QualityScore

    quality = QualityScore(lossless=lossless, bitrate=bitrate, sample_rate=44_100) if known else None
    return DuplicateCandidate(key=key, quality=quality, deletable=deletable)


def test_best_quality_names_the_better_file_even_when_it_could_not_be_kept_automatically():
    # "Italiano a Barcelona": a 128 kbps external MP3 and a 332 kbps OGG of ours.
    candidates = [_quality("external", bitrate=128_000), _quality("ours", bitrate=332_000, deletable=True)]
    assert decide_keeper(candidates).keeper_key is None  # keeping it would delete an external file
    assert best_quality_key(candidates) == "ours"


def test_best_quality_says_nothing_on_a_tie_or_an_unknown_quality():
    assert best_quality_key([_quality("a", bitrate=320_000), _quality("b", bitrate=320_000)]) is None
    assert best_quality_key([_quality("a", bitrate=320_000), _quality("b", known=False)]) is None
    assert best_quality_key([_quality("a", bitrate=320_000)]) is None


def test_best_quality_follows_the_lossless_preference():
    candidates = [_quality("flac", lossless=True, bitrate=900_000), _quality("mp3", bitrate=320_000)]
    assert best_quality_key(candidates, prefer_lossless=True) == "flac"
