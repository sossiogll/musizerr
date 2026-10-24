"""Matching cross-provider: ISRC/MBID esatti, fuzzy con guardie."""

from __future__ import annotations

from core.matching import find_match, match_album, match_artist, match_track
from musizeer_sdk.text import normalize, strip_version_suffix

from tests.conftest import make_album, make_artist, make_track


# ------------------------------------------------------------------
# normalize
# ------------------------------------------------------------------

def test_normalize_strips_punctuation_and_casefolds():
    assert normalize("Café — DELUXE!!") == "cafe deluxe"


def test_normalize_collapses_whitespace():
    assert normalize("  a   b\tc ") == "a b c"


def test_normalize_unicode_dashes_and_quotes():
    assert normalize("Rock’n–Roll") == normalize("Rock n Roll")


def test_normalize_folds_cyrillic_homoglyph():
    # La Я di "KoЯn" è cirillica ma sta per una R: va normalizzata a "korn",
    # non a "kon" (che è ciò che darebbe il semplice drop dei non-ASCII).
    assert normalize("KoЯn") == "korn"
    assert normalize("KoЯn") == normalize("Korn")


# ------------------------------------------------------------------
# strip_version_suffix
# ------------------------------------------------------------------

def test_strip_version_suffix_dash_tail():
    assert strip_version_suffix("Paranoid - 2012 Remaster") == "Paranoid"


def test_strip_version_suffix_parenthetical_and_tail():
    assert strip_version_suffix("Breathe (feat. Sean Paul) - Rap Version") == "Breathe"


def test_strip_version_suffix_keeps_plain_title():
    assert strip_version_suffix("Whole Lotta Love") == "Whole Lotta Love"


# ------------------------------------------------------------------
# match_track
# ------------------------------------------------------------------

def test_track_shared_isrc_is_exact():
    a = make_track("Song", "Artist", isrc="GBAYE9200715", provider="p1")
    b = make_track("Different Title", "Other", isrc="GBAYE9200715", provider="p2")
    result = match_track(a, b)
    assert result.matched and result.confidence == "exact"
    assert result.reason == "shared_isrc"


def test_track_title_mismatch_rejected():
    a = make_track("Song One", "Artist")
    b = make_track("Totally Other", "Artist")
    assert not match_track(a, b).matched


def test_track_artist_mismatch_rejected():
    a = make_track("Song", "Artist A")
    b = make_track("Song", "Completely Different")
    assert not match_track(a, b).matched


def test_track_stylized_artist_matches_via_homoglyph():
    # "KoЯn" (Я cirillica) deve agganciare "Korn": stesso titolo + album conferma.
    a = make_track("Hopeless And Beaten", "KoЯn", album="Issues")
    b = make_track("Hopeless And Beaten", "Korn", album="Issues")
    assert match_track(a, b).matched


def test_track_matches_on_any_artist_not_just_primary():
    # Un lato elenca più artisti (feat.), l'altro solo il principale.
    a = make_track("Breathe", "Blu Cantrell, Sean Paul", album="So Blu")
    b = make_track("Breathe", "Blu Cantrell", album="So Blu")
    assert match_track(a, b).matched


def test_track_version_suffix_is_ignored():
    a = make_track("Paranoid - 2012 - Remaster", "Black Sabbath", album="Paranoid")
    b = make_track("Paranoid", "Black Sabbath", album="Paranoid")
    assert match_track(a, b).matched


def test_track_same_title_wrong_artist_still_rejected():
    # Protezione dai falsi positivi: stesso titolo E stesso album, ma artista
    # diverso → NON è un match (niente aggancio "solo per titolo").
    a = make_track("Hopeless And Beaten", "Taylor Swift", album="Issues")
    b = make_track("Hopeless And Beaten", "Korn", album="Issues")
    assert not match_track(a, b).matched


# ------------------------------------------------------------------
# find_match — selezione del candidato (flusso usato dai provider)
# ------------------------------------------------------------------

def test_find_match_returns_the_matching_candidate():
    source = make_track("Song", "Artist", album="Album")
    wrong = make_track("Other Song", "Artist", album="Album")
    right = make_track("Song", "Artist", album="Album", provider_id="right")
    assert find_match(source, [wrong, right]) is right


def test_find_match_none_when_nothing_matches():
    source = make_track("Song", "Artist", album="Album")
    other = make_track("Song", "Different Artist", album="Album")
    assert find_match(source, [other]) is None


def test_find_match_skips_wrong_album_even_with_same_title_artist():
    source = make_track("Song", "Artist", album="Album A")
    wrong = make_track("Song", "Artist", album="Album B")
    right = make_track("Song", "Artist", album="Album A", provider_id="right")
    assert find_match(source, [wrong, right]) is right


# ------------------------------------------------------------------
# match_album
# ------------------------------------------------------------------

def test_album_shared_mbid_is_exact():
    a = make_album("X", "Artist", mbid="mbid-1", provider="p1")
    b = make_album("Y", "Zzz", mbid="mbid-1", provider="p2")
    assert match_album(a, b).matched


def test_album_year_mismatch_rejected():
    a = make_album("Greatest Hits", "Artist", year=1999)
    b = make_album("Greatest Hits", "Artist", year=2010)
    assert not match_album(a, b).matched


# ------------------------------------------------------------------
# match_artist
# ------------------------------------------------------------------

def test_artist_normalized_name_matches():
    a = make_artist("The Beatles")
    b = make_artist("the beatles")
    assert match_artist(a, b).matched


def test_artist_name_mismatch_rejected():
    assert not match_artist(make_artist("A"), make_artist("B")).matched
