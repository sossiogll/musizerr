"""Helper universale track_from_record: ricostruzione Track dal DB."""

from __future__ import annotations

import json

from db.models import TrackRecord
from db.track_store import track_from_record


def _record(**over) -> TrackRecord:
    base = dict(internal_id="t1", title="Song", artist="A", album="Al", duration_ms=1000)
    base.update(over)
    return TrackRecord(**base)


def test_uses_track_data_for_full_fidelity():
    td = {
        "internal_id": "t1", "title": "Song", "artist": "A", "album": "Al",
        "duration_ms": 1000, "year": 2001,
        "artists": [
            {"name": "A", "provider": "p", "provider_id": "1"},
            {"name": "B", "provider": "p", "provider_id": "2"},
        ],
    }
    track = track_from_record(_record(track_data=json.dumps(td)))
    assert track.year == 2001
    assert {a.name for a in track.artists} == {"A", "B"}


def test_falls_back_to_basic_fields_without_track_data():
    track = track_from_record(_record(track_data=None, duration_ms=500))
    assert track.internal_id == "t1"
    assert track.title == "Song"
    assert track.duration_ms == 500


def test_survives_corrupt_track_data():
    track = track_from_record(_record(track_data="{ not valid json"))
    assert track.title == "Song"  # fallback ai campi base, niente crash
