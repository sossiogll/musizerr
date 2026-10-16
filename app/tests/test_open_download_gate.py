"""Una traccia ha al più una richiesta di download aperta.

Caso reale che ha motivato la regola: una richiesta delegata restava "in
corso" per giorni, l'utente la rilanciava e un secondo provider scaricava la
stessa traccia in un'altra cartella — due copie dello stesso brano in libreria.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from core import download_states as ds
from services.config_manager import ConfigManager
from musizerr_sdk.models import Track
from core.user_auth import hash_password
from db.download_store import add_follower, find_open_download, latest_landing
from db.models import Download, DownloadFollower, PendingLike, TrackIdentifierRecord, TrackRecord, UserFavorite
from db.pending_like_store import ensure_pending_like
from db.user_service import create_user
from services import downloads
from routes.deps import Downloader
from routes.history import retry_download
from tests.conftest import FakeRegistry
from tests.database import seed_parents
from workers import maintenance


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def config(engine, monkeypatch):
    cm = ConfigManager(engine=engine)
    cm.update({"auth_enabled": True, "sync_provider": "sync"})
    monkeypatch.setattr(downloads, "get_config", lambda: cm)
    return cm


@pytest.fixture
def dispatched(monkeypatch):
    """Cattura i task accodati invece di mandarli a Celery."""
    calls: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: calls.append(kw))
    return calls


def _user(session, name: str):
    return create_user(session, username=name, password_hash=hash_password("x"), role="user")


def _retry(track_id, session, user, config):
    return retry_download(track_id, session, Downloader(user), FakeRegistry(), config, add_to_favorites=None, force=False)


def _track(session) -> str:
    record = TrackRecord(title="Snow (Hey Oh)", artist="Red Hot Chili Peppers", album="Stadium Arcadium", duration_ms=334000)
    session.add(record)
    session.commit()
    return record.internal_id


def _download_row(session, track_id: str, status: str, *, requested_by=None, handle=None, completed_at=None) -> str:
    dl = Download(
        track_id=track_id, status=status, requested_by=requested_by,
        delegation_handle=handle, completed_at=completed_at,
    )
    session.add(dl)
    session.commit()
    return dl.id


# ------------------------------------------------------------------
# Cosa conta come "aperta"
# ------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted(ds.OPEN - {ds.DISPATCHED}))
def test_every_non_terminal_state_is_open(session, status):
    tid = _track(session)
    dl_id = _download_row(session, tid, status)
    assert find_open_download(session, tid).id == dl_id


@pytest.mark.parametrize("status", sorted(ds.TERMINAL))
def test_terminal_states_are_not_open(session, status):
    tid = _track(session)
    _download_row(session, tid, status)
    assert find_open_download(session, tid) is None


def test_delegated_request_with_a_handle_is_open(session):
    tid = _track(session)
    dl_id = _download_row(session, tid, ds.DISPATCHED, handle="1225")
    assert find_open_download(session, tid).id == dl_id


def test_delegated_request_without_a_handle_does_not_block_the_track(session):
    # Righe precedenti alla riconciliazione: nessuno può più chiederne l'esito,
    # contarle come aperte bloccherebbe la traccia per sempre.
    tid = _track(session)
    _download_row(session, tid, ds.DISPATCHED, handle=None)
    assert find_open_download(session, tid) is None


def test_latest_landing_is_the_most_recent_successful_download(session):
    tid = _track(session)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    new = datetime(2026, 9, 10, tzinfo=timezone.utc)
    _download_row(session, tid, ds.DOWNLOADED, completed_at=old)
    _download_row(session, tid, ds.DOWNLOADED, completed_at=new)
    _download_row(session, tid, ds.FAILED, completed_at=datetime(2026, 9, 15))
    assert latest_landing(session, tid) == new


# ------------------------------------------------------------------
# Nuova richiesta e retry si agganciano a quella aperta
# ------------------------------------------------------------------

def test_a_second_request_attaches_instead_of_downloading_again(session, config, dispatched):
    user = _user(session, "dasert12")
    track = Track(title="Snow (Hey Oh)", artist="Red Hot Chili Peppers", album="Stadium Arcadium", duration_ms=334000)

    first = downloads.enqueue_track(track, session, user_id=user.id)
    second = downloads.enqueue_track(track, session, user_id=user.id)

    assert (second.status, second.download_id) == (downloads.ATTACHED, first.download_id)
    assert len(dispatched) == 1
    assert len(session.exec(Download.__table__.select()).all()) == 1


def test_a_request_after_the_previous_one_ended_downloads_again(session, config, dispatched):
    user = _user(session, "dasert12")
    track = Track(title="Snow (Hey Oh)", artist="Red Hot Chili Peppers", album="Stadium Arcadium", duration_ms=334000)

    first = downloads.enqueue_track(track, session, user_id=user.id).download_id
    row = session.get(Download, first)
    row.status = ds.FAILED
    session.add(row)
    session.commit()

    second = downloads.enqueue_track(track, session, user_id=user.id).download_id
    assert second != first
    assert len(dispatched) == 2


def test_retry_attaches_to_an_open_request(session, config, dispatched):
    user = _user(session, "dasert12")
    tid = _track(session)
    open_id = _download_row(session, tid, ds.DISPATCHED, requested_by=user.id, handle="1225")

    result = _retry(tid, session, user, config)

    assert result == {"status": downloads.ATTACHED, "download_id": open_id, "library_id": tid}
    assert dispatched == []


def test_retry_of_a_failed_request_still_downloads(session, config, dispatched):
    user = _user(session, "dasert12")
    tid = _track(session)
    failed_id = _download_row(session, tid, ds.FAILED, requested_by=user.id)

    result = _retry(tid, session, user, config)

    assert result["download_id"] != failed_id
    assert len(dispatched) == 1


def test_retry_sends_the_full_track_data_not_just_the_bare_columns(session, config, dispatched):
    """Il retry manda la traccia completa salvata al primo enqueue: anno e
    identifier con il loro provider, non solo le colonne base."""
    user = _user(session, "dasert12")
    tid = _track(session)

    record = session.get(TrackRecord, tid)
    record.track_data = json.dumps({
        "internal_id": tid,
        "title": record.title,
        "artist": record.artist,
        "album": record.album,
        "duration_ms": record.duration_ms,
        "cover_url": None,
        "preview_url": None,
        "stream_url": None,
        "provider": "provider-a",
        "provider_id": "abc123",
        "artist_provider_id": "",
        "album_provider_id": "",
        "year": 2006,
        "providers": ["provider-a"],
        "albums": [],
        "artists": [],
    })
    session.add(record)
    session.add(TrackIdentifierRecord(track_id=tid, id_type="provider-a_id", value="abc123"))
    session.add(TrackIdentifierRecord(track_id=tid, id_type="isrc", value="USRC10600001"))
    session.commit()

    failed_id = _download_row(session, tid, ds.FAILED, requested_by=user.id)

    result = _retry(tid, session, user, config)

    assert result["download_id"] != failed_id
    assert len(dispatched) == 1
    sent = dispatched[0]["track_data"]

    assert sent["year"] == 2006

    provider_ids = [i for i in sent["external_ids"] if i["type"] == "provider-a_id"]
    assert provider_ids == [{"type": "provider-a_id", "value": "abc123", "provider": "provider-a"}]

    rebuilt = Track.from_dict(sent)
    assert rebuilt.get_id("provider_id", "provider-a").value == "abc123"


# ------------------------------------------------------------------
# Chi si aggancia alla richiesta di un altro non perde il preferito
# ------------------------------------------------------------------

def test_attaching_to_another_users_request_keeps_the_followers_favorite(session, config, dispatched):
    owner = _user(session, "sossiogll")
    follower = _user(session, "dasert12")
    record = session.get(TrackRecord, _track(session))
    track = Track(title=record.title, artist=record.artist, album=record.album, duration_ms=record.duration_ms,
                  internal_id=record.internal_id)
    _download_row(session, record.internal_id, ds.DOWNLOADING, requested_by=owner.id)

    downloads.enqueue_track(track, session, user_id=follower.id, favorite_for=follower.id, registry=FakeRegistry())

    markers = session.exec(PendingLike.__table__.select()).all()
    assert [(m.user_id, m.track_id, m.sync_provider) for m in markers] == [(follower.id, record.internal_id, None)]
    assert dispatched == []  # agganciato, non riscaricato


def test_attaching_alone_registers_no_favorite(session, config, dispatched):
    owner = _user(session, "sossiogll")
    follower = _user(session, "dasert12")
    tid = _track(session)
    _download_row(session, tid, ds.DOWNLOADING, requested_by=owner.id)

    downloads.attach_to_open_download(session, tid, follower.id)

    assert session.exec(PendingLike.__table__.select()).all() == []


# ------------------------------------------------------------------
# Chi si aggancia alla richiesta di un altro la vede nella propria
# cronologia (DownloadFollower), senza duplicare l'aggancio.
# ------------------------------------------------------------------

def test_attaching_to_another_users_request_registers_a_follower(session, config, dispatched):
    owner = _user(session, "sossiogll")
    follower = _user(session, "dasert12")
    tid = _track(session)
    dl_id = _download_row(session, tid, ds.DOWNLOADING, requested_by=owner.id)

    result = downloads.attach_to_open_download(session, tid, follower.id)

    assert result == dl_id
    followers = session.exec(DownloadFollower.__table__.select()).all()
    assert [(f.download_id, f.user_id) for f in followers] == [(dl_id, follower.id)]


def test_attaching_twice_does_not_duplicate_the_follower_row(session, config, dispatched):
    owner = _user(session, "sossiogll")
    follower = _user(session, "dasert12")
    tid = _track(session)
    dl_id = _download_row(session, tid, ds.DOWNLOADING, requested_by=owner.id)

    downloads.attach_to_open_download(session, tid, follower.id)
    downloads.attach_to_open_download(session, tid, follower.id)

    followers = session.exec(DownloadFollower.__table__.select()).all()
    assert len(followers) == 1
    assert followers[0].download_id == dl_id
    assert followers[0].user_id == follower.id


def test_the_original_requester_attaching_to_their_own_request_is_not_a_follower(session, config, dispatched):
    # requested_by == user_id: non è un aggancio ad altrui, non deve
    # produrre una riga DownloadFollower (ne' un intento di like fantasma).
    owner = _user(session, "sossiogll")
    tid = _track(session)
    _download_row(session, tid, ds.DOWNLOADING, requested_by=owner.id)

    downloads.attach_to_open_download(session, tid, owner.id)

    assert session.exec(DownloadFollower.__table__.select()).all() == []


def test_add_follower_helper_is_idempotent(session):
    owner = _user(session, "sossiogll")
    follower = _user(session, "dasert12")
    tid = _track(session)
    dl_id = _download_row(session, tid, ds.DOWNLOADING, requested_by=owner.id)

    add_follower(session, dl_id, follower.id)
    add_follower(session, dl_id, follower.id)

    assert len(session.exec(DownloadFollower.__table__.select()).all()) == 1


def test_landing_makes_every_intent_on_the_track_a_favorite(session, config):
    from services import favorites

    tid = _track(session)
    seed_parents(session, users=["u1", "u2"], tracks=["other-track"])
    ensure_pending_like(session, "u1", tid)
    ensure_pending_like(session, "u2", tid)
    ensure_pending_like(session, "u1", "other-track")
    _download_row(session, tid, ds.DOWNLOADED)

    assert favorites.apply_intents(session, tid) == 2

    rows = session.exec(UserFavorite.__table__.select()).all()
    assert sorted((r.user_id, r.track_id, r.source) for r in rows) == [("u1", tid, "download"), ("u2", tid, "download")]
    # Senza sync provider non resta nulla da propagare: gli intenti si chiudono.
    assert [m.track_id for m in session.exec(PendingLike.__table__.select()).all()] == ["other-track"]


def test_like_age_is_counted_from_the_landing_when_it_is_later():
    registered = datetime.now(timezone.utc) - timedelta(days=3)
    landed = datetime.now(timezone.utc) - timedelta(minutes=5)
    age = maintenance._like_age_seconds(registered, landed)
    assert 290 < age < 310


def test_like_age_falls_back_to_registration_when_the_track_never_landed():
    registered = datetime.now(timezone.utc) - timedelta(minutes=5)
    age = maintenance._like_age_seconds(registered, None)
    assert 290 < age < 310
