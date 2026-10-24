"""``core.tagging.read_track_tags``: la Track descritta dai tag di un file."""
from __future__ import annotations

from musizeer_sdk.identifiers import Identifier
from core.tagging import read_track_tags
from tests.conftest import write_flac


def test_the_tags_describe_the_track(tmp_path):
    path = write_flac(
        tmp_path / "song.flac", seconds=3,
        title="Song", artist="Artist", album="Album", date="1999-05-01",
        tracknumber="3/12", discnumber="2", isrc="gbaye9200715", musicbrainz_trackid="rec-1",
    )

    track = read_track_tags(str(path))

    assert (track.title, track.artist, track.album) == ("Song", "Artist", "Album")
    assert track.duration_ms == 3000
    assert (track.year, track.track_number, track.disc_number) == (1999, 3, 2)
    assert track.external_ids == [
        Identifier(type="isrc", value="GBAYE9200715"),
        Identifier(type="mbid", value="rec-1"),
    ]


def test_missing_optional_tags_stay_unknown(tmp_path):
    track = read_track_tags(str(write_flac(tmp_path / "s.flac", title="Song", artist="Artist", tracknumber="0")))

    assert track.album == ""
    assert (track.year, track.track_number, track.disc_number) == (None, None, None)
    assert track.external_ids == []


def test_a_file_without_title_or_artist_describes_no_track(tmp_path):
    assert read_track_tags(str(write_flac(tmp_path / "a.flac", title="Song"))) is None
    assert read_track_tags(str(write_flac(tmp_path / "b.flac", artist="Artist"))) is None


def test_an_unreadable_file_describes_no_track(tmp_path):
    (tmp_path / "noise.mp3").write_bytes(b"not audio")

    assert read_track_tags(str(tmp_path / "noise.mp3")) is None
    assert read_track_tags(str(tmp_path / "missing.flac")) is None
