"""StaticProviderContext, the identifiers and the pure helpers a provider uses."""
from __future__ import annotations

import pytest

from musizeer_sdk.context import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_STREAM_START_TIMEOUT,
    ProviderContext,
    StaticProviderContext,
)
from musizeer_sdk.identifiers import ISRC, PROVIDER_ID, Identifier, is_sync_id_type
from musizeer_sdk.models import Track
from musizeer_sdk.text import (
    normalize,
    parse_bitrate,
    parse_sample_rate,
    parse_year,
    strip_version_suffix,
    strip_video_noise,
)


def test_the_static_context_is_a_provider_context_with_the_default_timeouts():
    context = StaticProviderContext()

    assert isinstance(context, ProviderContext)
    assert (context.http_timeout, context.download_timeout, context.connect_timeout, context.stream_start_timeout) == (
        DEFAULT_HTTP_TIMEOUT, DEFAULT_DOWNLOAD_TIMEOUT, DEFAULT_CONNECT_TIMEOUT, DEFAULT_STREAM_START_TIMEOUT,
    )


def test_the_static_context_serves_the_settings_it_was_given_read_only():
    context = StaticProviderContext({"url": "http://x"})

    assert context.setting("url") == "http://x" and context.setting("missing", 7) == 7
    assert dict(context.settings()) == {"url": "http://x"}
    with pytest.raises(TypeError):
        context.settings()["url"] = "other"  # type: ignore[index]


def test_an_identifier_survives_its_storage_form():
    catalog_like = Identifier(PROVIDER_ID, "abc", "some-provider")

    assert catalog_like.storage_type() == "some-provider_id"
    assert Identifier.from_dict(catalog_like.to_dict()) == catalog_like
    assert Identifier.from_dict({"type": "some-provider_id", "value": "abc"}) == catalog_like
    assert Identifier.from_dict(Identifier(ISRC, "X1").to_dict()) == Identifier(ISRC, "X1")


def test_a_sync_id_is_not_taken_for_the_id_of_a_provider():
    sync = Identifier.from_dict({"type": "library_sync_id", "value": "42"})

    assert (sync.type, sync.provider) == ("library_sync_id", None)
    assert is_sync_id_type("library_sync_id") and not is_sync_id_type("some-provider_id")


def test_a_track_survives_to_dict_and_from_dict():
    track = Track(title="T", artist="A", album="B", duration_ms=1000, year=1999, track_number=3)
    track.add_id(Identifier(ISRC, "X1"))

    back = Track.from_dict(track.to_dict())

    assert (back.title, back.artist, back.album, back.year, back.track_number) == ("T", "A", "B", 1999, 3)
    assert back.get_id(ISRC) == Identifier(ISRC, "X1")


@pytest.mark.parametrize("value, expected", [("1999-05-01", 1999), ("2001", 2001), ("19", None), (None, None), ("abcd", None)])
def test_parse_year(value, expected):
    assert parse_year(value) == expected


def test_normalize_folds_case_accents_punctuation_and_homoglyphs():
    assert normalize("  Beyoncé -- Halo!  ") == "beyonce halo"
    assert normalize("KoЯn") == "korn"


def test_strip_helpers_drop_the_decoration_but_keep_a_title_that_would_vanish():
    assert strip_video_noise("Song (Official Video) [HD]") == "Song"
    assert strip_video_noise("Song (Live)") == "Song (Live)"
    assert strip_version_suffix("Song - 2011 Remaster (feat. X)") == "Song"
    assert strip_version_suffix("(Intro)") == "(Intro)"


@pytest.mark.parametrize("value, bitrate, rate", [
    ("320 kbps", 320_000, 0), (256000, 256_000, 256000), ("44.1kHz", 0, 44_100), ("", 0, 0), (None, 0, 0), (True, 0, 0),
])
def test_unit_values_are_read_or_unknown(value, bitrate, rate):
    if "hz" not in str(value).lower():
        assert parse_bitrate(value) == bitrate
    if "bps" not in str(value).lower():
        assert parse_sample_rate(value) == rate


def test_a_static_context_gives_the_snapshot_a_call_carries():
    from musizeer_sdk.context import ContextSnapshot

    context = StaticProviderContext(
        settings={"market": "IT"}, http_timeout=1, download_timeout=2, connect_timeout=3, stream_start_timeout=4,
        api_prefix="/music",
    )

    assert context.snapshot() == ContextSnapshot(
        settings={"market": "IT"}, http_timeout=1, download_timeout=2, connect_timeout=3, stream_start_timeout=4,
        api_prefix="/music",
    )
    assert StaticProviderContext().snapshot() == ContextSnapshot()


def test_the_snapshot_is_a_copy_of_the_settings():
    context = StaticProviderContext(settings={"a": 1})

    context.snapshot().settings["a"] = 2

    assert context.setting("a") == 1


# --- dashes: one set of hyphen-like characters everywhere ---------------------

HYPHEN_LIKE = [
    "-", "‐", "‑", "‒", "–", "—", "―", "−",  # the ones named by the contract
    "﹘", "﹣", "－", "⁃",  # compatibility and hyphen-bullet forms
]


@pytest.mark.parametrize("dash", HYPHEN_LIKE)
def test_normalize_reads_every_hyphen_like_character_as_the_same_separator(dash):
    assert normalize(f"Backup 1987{dash}2012") == normalize("Backup 1987-2012") == "backup 1987 2012"
    assert normalize(f"Song {dash} Live") == normalize("Song - Live") == "song live"


def test_normalize_does_not_make_a_subtitle_the_same_title():
    assert normalize("Backup 1987-2012") != normalize("Backup 1987–2012: Il best")


@pytest.mark.parametrize("dash", HYPHEN_LIKE[:8])
def test_a_spaced_dash_of_any_kind_starts_a_version_tail(dash):
    assert strip_version_suffix(f"Paranoid {dash} 2012 Remaster") == "Paranoid"
    assert strip_version_suffix(f"Breathe (feat. Sean Paul) {dash} Rap Version") == "Breathe"


@pytest.mark.parametrize("dash", HYPHEN_LIKE[:8])
def test_a_dash_without_spaces_does_not_start_a_tail(dash):
    assert strip_version_suffix(f"Backup 1987{dash}2012") == f"Backup 1987{dash}2012"


@pytest.mark.parametrize("dash", HYPHEN_LIKE[:8])
def test_video_noise_after_a_spaced_dash_of_any_kind_is_stripped(dash):
    assert strip_video_noise(f"Song {dash} Official Video") == "Song"
    assert strip_video_noise(f"Song {dash} Radio Edit") == f"Song {dash} Radio Edit"
