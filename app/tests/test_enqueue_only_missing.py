"""
"Download the N missing" on an album or playlist: with ``only_missing`` the
tracks already in the library or on their way are skipped, the others are
queued; without it the tracks on their way are joined (open downloads are
reused) and the ones in the library are still never downloaded again.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from musizerr_sdk.models import Track
from db.models import Download
from db.track_store import find_or_create_track
from services import downloads
from tests.database import new_engine
from tests.conftest import FakeRegistry


@pytest.fixture
def session(monkeypatch):
    engine = new_engine()
    queued: list[str] = []
    monkeypatch.setattr(downloads, "queue_download", lambda track_data, download_id, user_id=None, force=False: queued.append(download_id))
    with Session(engine) as s:
        s.info["queued"] = queued
        yield s


def _track(n: int) -> Track:
    # As a metadata provider returns it: its own id among the identifiers.
    return Track(
        title=f"Song {n}", artist="Artist", album="Album", provider="prov", provider_id=f"p{n}", duration_ms=1000,
        external_ids=[Identifier(type=PROVIDER_ID, value=f"p{n}", provider="prov")],
    )


def _downloaded(session: Session, track: Track) -> None:
    record = find_or_create_track(track, session)
    session.add(Download(track_id=record.internal_id, status="downloaded"))
    session.commit()


def test_only_missing_skips_the_tracks_in_the_library(session):
    tracks = [_track(1), _track(2), _track(3)]
    _downloaded(session, tracks[1])

    batch = downloads.enqueue_tracks(tracks, session, FakeRegistry(), only_missing=True)
    ids = batch.download_ids

    assert batch.skipped == 1
    assert len(ids) == 2
    assert len(session.info["queued"]) == 2
    queued_track_ids = {session.get(Download, i).track_id for i in ids}
    assert find_or_create_track(tracks[1], session).internal_id not in queued_track_ids


def test_a_failed_track_is_missing_and_queued_again(session):
    tracks = [_track(1)]
    record = find_or_create_track(tracks[0], session)
    session.add(Download(track_id=record.internal_id, status="failed"))
    session.commit()

    batch = downloads.enqueue_tracks(tracks, session, FakeRegistry(), only_missing=True)

    assert (len(batch.download_ids), batch.skipped) == (1, 0)


def test_without_the_flag_the_tracks_in_the_library_are_still_never_downloaded_again(session):
    """Without ``only_missing`` a failed or never requested track is queued;
    one already in the library never is (``enqueue_track`` refuses it)."""
    tracks = [_track(1), _track(2)]
    _downloaded(session, tracks[0])

    batch = downloads.enqueue_tracks(tracks, session, FakeRegistry())

    assert (len(batch.download_ids), batch.skipped) == (1, 1)
    assert len(session.exec(select(Download)).all()) == 2
