"""
The SQL that read differently on the two databases, now written to read the
same: the order of a nullable column (SQLite puts NULL first when ascending,
PostgreSQL last), a number inside JSON, a case-insensitive match, and what a
sort on them gives a route.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from db.download_store import latest_landing
from db.expressions import ascending, descending, json_number
from db.library_store import stored_file_paths
from db.models import Download, TrackRecord
from db.track_store import find_or_create_track

from tests.conftest import make_track
from tests.database import seed_parents


def _tracks(session, *titles_and_data) -> list[str]:
    for title, data in titles_and_data:
        session.add(TrackRecord(title=title, artist="A", album="B", track_data=data))
    session.commit()


def test_a_nullable_column_sorts_with_null_as_the_smallest_value(engine):
    with Session(engine) as session:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        seed_parents(session, tracks=["t"])
        for title, completed in (("never", None), ("old", base), ("new", base + timedelta(days=5))):
            session.add(Download(track_id="t", status=ds.DOWNLOADED, file_path=title, completed_at=completed))
        session.commit()

        ascending_order = [d.file_path for d in session.exec(
            select(Download).order_by(ascending(Download.completed_at)))]
        descending_order = [d.file_path for d in session.exec(
            select(Download).order_by(descending(Download.completed_at)))]

    assert ascending_order == ["never", "old", "new"]
    assert descending_order == ["new", "old", "never"]


def test_the_newest_landing_ignores_a_row_without_a_date(engine):
    with Session(engine) as session:
        seed_parents(session, tracks=["t"])
        landed = datetime(2026, 5, 1, tzinfo=timezone.utc)
        session.add(Download(track_id="t", status=ds.DOWNLOADED, file_path="dated.flac", completed_at=landed))
        session.add(Download(track_id="t", status=ds.DOWNLOADED, file_path="undated.flac", completed_at=None))
        session.commit()

        assert latest_landing(session, "t") == landed
        assert stored_file_paths(session, "t") == ["dated.flac", "undated.flac"]


def test_json_number_reads_a_number_and_nothing_else(engine):
    with Session(engine) as session:
        _tracks(
            session,
            ("year", json.dumps({"year": 1999})),
            ("float", json.dumps({"year": 2001.5})),
            ("text", json.dumps({"year": "unknown"})),
            ("null", json.dumps({"year": None})),
            ("missing", json.dumps({"title": "x"})),
            ("no data", None),
        )

        values = {t.title: v for t, v in session.exec(select(TrackRecord, json_number(TrackRecord.track_data, "year")))}

    assert values == {"year": 1999, "float": 2001.5, "text": None, "null": None, "missing": None, "no data": None}


def test_a_sort_by_year_puts_the_unknown_ones_first_ascending_and_last_descending(engine):
    with Session(engine) as session:
        _tracks(
            session,
            ("b", json.dumps({"year": 2010})), ("a", json.dumps({"year": 1990})), ("n", json.dumps({})),
            ("c", json.dumps({"year": 2022})),
        )
        year = json_number(TrackRecord.track_data, "year")

        up = [t.title for t in session.exec(select(TrackRecord).order_by(ascending(year), TrackRecord.title))]
        down = [t.title for t in session.exec(select(TrackRecord).order_by(descending(year), TrackRecord.title))]

    assert up == ["n", "a", "b", "c"]
    assert down == ["c", "b", "a", "n"]


def test_a_sort_by_year_is_not_confused_by_a_text_year(engine):
    with Session(engine) as session:
        _tracks(session, ("x", json.dumps({"year": "1985"})), ("y", json.dumps({"year": 2000})))
        year = json_number(TrackRecord.track_data, "year")

        titles = [t.title for t in session.exec(select(TrackRecord).order_by(ascending(year), TrackRecord.title))]

    assert titles == ["x", "y"]  # the text year is unknown, so first


def test_a_json_key_is_a_plain_identifier():
    with pytest.raises(ValueError):
        json_number(TrackRecord.track_data, "year') OR ('1'='1")


def test_the_fuzzy_match_of_a_title_ignores_case(engine):
    with Session(engine) as session:
        stored = find_or_create_track(make_track("two", "Artist", duration_ms=3_000), session)

        found = find_or_create_track(make_track("Two", "ARTIST", duration_ms=3_000, provider="other"), session)

        assert found.internal_id == stored.internal_id
        assert len(session.exec(select(TrackRecord)).all()) == 1


# ---------------------------------------------------------------------------
# The download history, sorted
# ---------------------------------------------------------------------------


def _history(engine, monkeypatch, sort: str) -> list[str]:
    from routes import history
    from services.capabilities import Viewer
    from db.models import User
    from tests.conftest import FakeRegistry

    monkeypatch.setattr(history, "enrich_dicts_with_stream_url", lambda *a, **k: None)
    with Session(engine) as session:
        admin = User(username="admin", password_hash="x", role="admin")
        session.add(admin)
        session.commit()
        for title, artist, started in (
            ("b-song", "z-artist", datetime(2026, 1, 2, tzinfo=timezone.utc)),
            ("a-song", "y-artist", datetime(2026, 1, 3, tzinfo=timezone.utc)),
            ("c-song", "x-artist", None),  # asked for, not started yet
        ):
            record = TrackRecord(title=title, artist=artist, album="Album")
            session.add(record)
            session.flush()
            session.add(Download(track_id=record.internal_id, status=ds.PENDING if started is None else ds.FAILED,
                                 started_at=started))
        session.commit()
        page = history.get_downloads(
            session, FakeRegistry(), auth_manager=object(), user=admin,
            viewer=Viewer(user_id=admin.id, stream_full=True),
            page=1, limit=50, status=None, q=None, sort=sort, phase=None,
        )
    return [item["track"]["title"] for item in page["items"]]


@pytest.mark.parametrize("sort, expected", [
    ("recent", ["a-song", "b-song", "c-song"]),  # newest first, the one never started last
    ("title", ["a-song", "b-song", "c-song"]),
    ("artist", ["c-song", "a-song", "b-song"]),
])
def test_the_history_sorts_the_same_on_both_databases(engine, monkeypatch, sort, expected):
    assert _history(engine, monkeypatch, sort) == expected
