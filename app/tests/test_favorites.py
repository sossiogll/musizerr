"""
Favorites and the downloads that feed them, in the four viewer modes
(guest / user / admin / open = auth disabled):

- a user's download ALWAYS creates the "favorite on arrival" intent, through
  every route that queues one, and no parameter turns it off; an admin (and
  open mode) chooses: plain by default, with favorite on request;
- the intent becomes a favorite when the track lands, and survives a failure;
- the heart only works on tracks in the library (409 ``not_in_library``),
  is idempotent, and removing a favorite never touches the file;
- "add to favorites" on a collection adds only the tracks in the library.

Real routes through TestClient, SQLite in memory, fake providers only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core import download_states as ds
from core.download_policy import KINDS, MODE_AUTO, mode_key
from core.user_auth import create_access_token
from db.engine import get_session
from db.models import (
    SYSTEM_USER_ID,
    Download,
    LibraryEntry,
    PendingLike,
    TrackRecord,
    User,
    UserFavorite,
)
from routes import deps
from routes.content.album import router as album_router
from routes.content.artist import router as artist_router
from routes.content.playlist import router as playlist_router
from routes.content.track import router as track_router
from routes.errors import install_error_handlers
from routes.history import router as history_router
from routes.library import router as library_router
from services import downloads, favorites
from services.config_manager import ConfigManager

from tests.conftest import FakeAuthManager, FakeRegistry
from tests.database import seed_system_user
from tests.test_download_permissions import _Catalog
from tests.test_openapi_responses import assert_conforms

MODES = ("guest", "user", "admin", "open")


def _user(engine, role: str) -> User:
    with Session(engine) as session:
        user = User(username=f"{role}-u", password_hash="x", role=role)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


class _Api:
    def __init__(self, client: TestClient, headers: dict, owner: str | None, queued: list):
        self.client = client
        self.headers = headers
        self.owner = owner  # whose favorites these are (None for a guest)
        self.queued = queued

    def request(self, method: str, url: str, **kwargs):
        return self.client.request(method, url, headers=self.headers, **kwargs)


def _build(engine, monkeypatch, mode: str) -> _Api:
    config = ConfigManager(engine=engine)
    # About favorites, not approval: every kind is approved at once
    # (``tests/test_download_requests.py`` covers the policy).
    config.update({"auth_enabled": mode != "open", **{mode_key(kind): MODE_AUTO for kind in KINDS}})
    monkeypatch.setattr(downloads, "get_config", lambda: config)
    queued: list[dict] = []
    monkeypatch.setattr(downloads, "queue_download", lambda **kw: queued.append(kw))

    app = FastAPI()
    install_error_handlers(app)
    for router in (track_router, album_router, artist_router, playlist_router, history_router, library_router):
        app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    registry = FakeRegistry(metadata=[_Catalog()])
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[deps.get_config] = lambda: config
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_auth_manager] = lambda: FakeAuthManager()

    headers, owner = {}, None
    if mode in ("user", "admin"):
        user = _user(engine, mode)
        headers = {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
        owner = user.id
    elif mode == "open":
        owner = SYSTEM_USER_ID
        seed_system_user(engine)
    return _Api(TestClient(app), headers, owner, queued)


@pytest.fixture(params=MODES)
def api(request, engine, monkeypatch) -> _Api:
    api = _build(engine, monkeypatch, request.param)
    api.mode = request.param
    return api


@pytest.fixture
def make_api(engine, monkeypatch):
    return lambda mode: _build(engine, monkeypatch, mode)


def _intents(engine) -> list[PendingLike]:
    with Session(engine) as session:
        return list(session.exec(select(PendingLike)).all())


def _favorites(engine) -> list[UserFavorite]:
    with Session(engine) as session:
        return list(session.exec(select(UserFavorite)).all())


def _track_in_library(engine, *, how: str = "download", title: str = "In library") -> str:
    with Session(engine) as session:
        record = TrackRecord(title=title, artist="Artist", album="Album")
        session.add(record)
        session.commit()
        if how == "download":
            session.add(Download(track_id=record.internal_id, status=ds.DOWNLOADED, file_path="A/In.flac"))
        elif how == "scan":
            session.add(LibraryEntry(track_id=record.internal_id, file_path=f"A/{title}.flac", file_format="flac",
                                     source="scan", storage_provider="fakestorage"))
        session.commit()
        return record.internal_id


def _track_not_in_library(engine, status: str | None = None, requested_by: str | None = None) -> str:
    with Session(engine) as session:
        record = TrackRecord(title="Elsewhere", artist="Artist", album="Album")
        session.add(record)
        session.commit()
        if status:
            session.add(Download(track_id=record.internal_id, status=status, requested_by=requested_by))
            session.commit()
        return record.internal_id


# ---------------------------------------------------------------------------
# Download: intent per mode, on every route
# ---------------------------------------------------------------------------

_BODY = {"provider": "catalog", "id": "x"}
_COLLECTION_ROUTES = ("/download/track", "/download/album", "/download/playlist", "/download/artist")


@pytest.mark.parametrize("route", _COLLECTION_ROUTES)
@pytest.mark.parametrize("requested", [None, False, True])
def test_download_creates_the_intent_exactly_when_the_rule_says(api, engine, route, requested):
    body = dict(_BODY) if requested is None else {**_BODY, "add_to_favorites": requested}

    resp = api.request("POST", route, json=body)

    if api.mode == "guest":
        assert resp.status_code == 401
        assert _intents(engine) == [] and api.queued == []
        return
    assert resp.status_code == 200, resp.text
    assert len(api.queued) == 1
    intents = _intents(engine)
    # A user: always, whatever the body says. Admin/open: only when asked.
    wanted = api.mode == "user" or requested is True
    assert [(i.user_id, i.sync_provider) for i in intents] == ([(api.owner, None)] if wanted else [])


def test_a_user_cannot_turn_the_favorite_off_even_explicitly(make_api, engine):
    api = make_api("user")
    for route in _COLLECTION_ROUTES:
        api.request("POST", route, json={**_BODY, "add_to_favorites": False, "only_missing": False})
    assert {(i.user_id, i.sync_provider) for i in _intents(engine)} == {(api.owner, None)}


@pytest.mark.parametrize("requested", [None, "false", "true"])
def test_retry_and_bulk_retry_follow_the_same_rule(api, engine, requested):
    failed = _track_not_in_library(engine, status=ds.FAILED, requested_by=api.owner)
    query = "" if requested is None else f"?add_to_favorites={requested}"

    single = api.request("POST", f"/downloads/{failed}/retry{query}")
    if api.mode == "guest":
        assert single.status_code == 401
        return
    assert single.status_code == 200
    wanted = api.mode == "user" or requested == "true"
    assert [(i.user_id, i.track_id) for i in _intents(engine)] == ([(api.owner, failed)] if wanted else [])


_ANNOTATED_FIELDS = {"in_library", "download_status", "favorite", "favorite_pending", "playback", "library_id"}


def _response_tracks(route: str, body: dict) -> list[dict]:
    if route == "/download/track":
        return [body["track"]]
    return body["album" if route == "/download/album" else "playlist"]["tracks"]


@pytest.mark.parametrize("route", ("/download/track", "/download/album", "/download/playlist"))
@pytest.mark.parametrize(("mode", "requested", "pending"), [
    ("user", None, True),      # a user's download always favorites on arrival
    ("user", False, True),     # ... even when the body says no
    ("admin", None, False),    # an admin's plain download
    ("admin", False, False),
    ("admin", True, True),     # an admin who asked for the favorite
])
def test_the_download_response_carries_the_track_state(make_api, route, mode, requested, pending):
    """The response annotates its tracks like every track list, so the row
    shows the pending heart from the response, before any refetch."""
    api = make_api(mode)
    body = dict(_BODY) if requested is None else {**_BODY, "add_to_favorites": requested}

    resp = api.request("POST", route, json=body)

    assert resp.status_code == 200, resp.text
    assert_conforms(resp, "POST", route)
    tracks = _response_tracks(route, resp.json())
    assert tracks
    for track in tracks:
        assert _ANNOTATED_FIELDS <= track.keys()
        assert track["library_id"]
        assert track["download_status"] == ds.PENDING
        assert track["in_library"] is False and track["favorite"] is False
        assert track["favorite_pending"] is pending


def test_a_download_of_a_track_in_the_library_answers_the_favorite(make_api, engine):
    """Already in the library: no new download, the favorite is applied at
    once and the response says so (not a pending heart)."""
    api = make_api("user")
    first = api.request("POST", "/download/track", json=_BODY).json()
    with Session(engine) as session:
        session.add(Download(track_id=first["track"]["library_id"], status=ds.DOWNLOADED, file_path="A/x.flac"))
        session.commit()

    resp = api.request("POST", "/download/track", json=_BODY)

    assert resp.status_code == 200
    assert resp.json()["status"] == downloads.ALREADY_IN_LIBRARY
    track = resp.json()["track"]
    assert (track["in_library"], track["favorite"], track["favorite_pending"]) == (True, True, False)


def test_bulk_retry_of_a_user_adds_their_favorite_intent(make_api, engine):
    api = make_api("user")
    with Session(engine) as session:
        record = TrackRecord(title="Broken", artist="A", album="B")
        session.add(record)
        session.commit()
        session.add(Download(track_id=record.internal_id, status=ds.FAILED, requested_by=api.owner))
        session.commit()
        broken = record.internal_id

    resp = api.request("POST", "/downloads/retry-failed?add_to_favorites=false")

    assert resp.status_code == 200 and len(resp.json()["download_ids"]) == 1
    assert [(i.user_id, i.track_id) for i in _intents(engine)] == [(api.owner, broken)]


def test_the_library_download_of_missing_likes_always_favorites(monkeypatch, engine):
    """``POST /library/download-sync``: the likes of the user, downloaded,
    arrive as their favorites (the worker passes ``favorite_for``)."""
    from db.models import UserSavedTrack
    from workers import tasks

    user = _user(engine, "user")
    liked = _track_not_in_library(engine)
    with Session(engine) as session:
        record = session.get(TrackRecord, liked)
        record.track_data = '{"title": "Elsewhere", "artist": "Artist", "album": "Album", "duration_ms": 1}'
        session.add(record)
        session.add(UserSavedTrack(user_id=user.id, track_id=liked))
        session.commit()

    calls: list[dict] = []
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "get_config", lambda: ConfigManager(engine=engine))
    monkeypatch.setattr(downloads, "enqueue_track", lambda track, session, **kw: calls.append(kw))

    tasks.library_download_sync.run(user_id=user.id)

    assert [c["favorite_for"] for c in calls] == [user.id]


# ---------------------------------------------------------------------------
# Arrival: the intent becomes a favorite; a failure keeps it
# ---------------------------------------------------------------------------


def test_the_like_is_applied_when_the_track_lands(monkeypatch, engine):
    from workers import tasks

    user = _user(engine, "user")
    track_id = _track_not_in_library(engine)
    with Session(engine) as session:
        favorites.register_intent(session, user.id, track_id, FakeRegistry())
        dl = Download(track_id=track_id, status=ds.DOWNLOADED, requested_by=user.id)
        session.add(dl)
        session.commit()
        download_id = dl.id
    monkeypatch.setattr(tasks, "engine", engine)

    tasks._on_landed(download_id)

    assert [(f.user_id, f.track_id, f.source) for f in _favorites(engine)] == [(user.id, track_id, "download")]
    assert _intents(engine) == []


def test_the_like_is_propagated_to_the_sync_provider_on_arrival(engine, monkeypatch, dispatched_tasks):
    monkeypatch.setattr(favorites, "get_config", lambda: SimpleNamespace(sync_provider="fakesync"))
    user = _user(engine, "user")
    track_id = _track_not_in_library(engine)
    with Session(engine) as session:
        favorites.register_intent(session, user.id, track_id, FakeRegistry())
        session.add(Download(track_id=track_id, status=ds.DOWNLOADED))
        session.commit()
        favorites.apply_intents(session, track_id)

    (intent,) = _intents(engine)
    assert intent.sync_provider == "fakesync"  # the like still has to reach the sync provider
    assert dispatched_tasks == [("workers.maintenance.reconcile_like", {"pending_like_id": intent.id})]


def test_a_failed_download_keeps_the_intent_and_hides_the_pending_heart(make_api, engine):
    api = make_api("user")
    track_id = _track_not_in_library(engine, status=ds.DOWNLOADING, requested_by=api.owner)
    with Session(engine) as session:
        favorites.register_intent(session, api.owner, track_id, FakeRegistry())

    # While it downloads: the pending heart.
    row = _row(api, track_id)
    assert row["favorite_pending"] is True and row["favorite"] is False

    with Session(engine) as session:
        dl = session.exec(select(Download).where(Download.track_id == track_id)).one()
        dl.status = ds.FAILED
        session.add(dl)
        session.commit()

    row = _row(api, track_id)
    assert row["download_status"] == ds.FAILED
    assert row["favorite_pending"] is False
    assert [(i.user_id, i.track_id) for i in _intents(engine)] == [(api.owner, track_id)]


def test_the_reconciler_applies_an_intent_once_the_track_is_in_the_library(monkeypatch, engine):
    from workers import maintenance

    user = _user(engine, "user")
    track_id = _track_not_in_library(engine, status=ds.FAILED)
    with Session(engine) as session:
        favorites.register_intent(session, user.id, track_id, FakeRegistry())
        intent_id = session.exec(select(PendingLike.id)).one()
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(maintenance, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(maintenance, "get_config", lambda: SimpleNamespace(get=lambda k, d=None: d))

    assert maintenance.reconcile_like.run(pending_like_id=intent_id)["status"] == "awaiting-download"
    assert _favorites(engine) == []

    with Session(engine) as session:
        session.add(Download(track_id=track_id, status=ds.DOWNLOADED))
        session.commit()
    assert maintenance.reconcile_like.run(pending_like_id=intent_id)["status"] == "favorited"
    assert [(f.user_id, f.track_id) for f in _favorites(engine)] == [(user.id, track_id)]


def _row(api: _Api, track_id: str) -> dict:
    items = api.request("GET", "/downloads").json()["items"]
    return next(item["track"] for item in items if item["track_id"] == track_id)


# ---------------------------------------------------------------------------
# The heart
# ---------------------------------------------------------------------------


def test_the_heart_per_mode(api, engine):
    in_library = _track_in_library(engine)

    put = api.request("PUT", f"/favorites/{in_library}")
    if api.mode == "guest":
        assert put.status_code == 401
        assert api.request("DELETE", f"/favorites/{in_library}").status_code == 401
        return
    assert put.status_code == 200
    assert_conforms(put, "PUT", "/favorites/{track_id}")
    assert put.json() == {"track_id": in_library, "favorite": True, "changed": True}
    assert [(f.user_id, f.track_id, f.source) for f in _favorites(engine)] == [(api.owner, in_library, "heart")]


def test_the_heart_rejects_a_track_not_in_the_library(make_api, engine):
    api = make_api("user")
    for status in (None, ds.PENDING, ds.DOWNLOADING, ds.FAILED):
        track_id = _track_not_in_library(engine, status=status)
        resp = api.request("PUT", f"/favorites/{track_id}")
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "not_in_library"
        assert_conforms(resp, "PUT", "/favorites/{track_id}")
    assert api.request("PUT", "/favorites/no-such-track").status_code == 404
    assert _favorites(engine) == []


@pytest.mark.parametrize("how", ["download", "scan"])
def test_the_heart_is_idempotent(make_api, engine, how):
    api = make_api("user")
    track_id = _track_in_library(engine, how=how)

    assert api.request("PUT", f"/favorites/{track_id}").json()["changed"] is True
    assert api.request("PUT", f"/favorites/{track_id}").json()["changed"] is False
    assert len(_favorites(engine)) == 1

    state = api.request("GET", f"/favorites/{track_id}")
    assert_conforms(state, "GET", "/favorites/{track_id}")
    assert state.json() == {"track_id": track_id, "favorite": True, "in_library": True}

    removed = api.request("DELETE", f"/favorites/{track_id}")
    assert_conforms(removed, "DELETE", "/favorites/{track_id}")
    assert removed.json() == {"track_id": track_id, "favorite": False, "changed": True}
    assert api.request("DELETE", f"/favorites/{track_id}").json()["changed"] is False
    assert _favorites(engine) == []
    assert api.request("GET", f"/favorites/{track_id}").json()["favorite"] is False


def test_unlike_keeps_the_file_and_takes_the_like_back(make_api, engine, monkeypatch, dispatched_tasks):
    monkeypatch.setattr(favorites, "get_config", lambda: SimpleNamespace(sync_provider="fakesync"))
    api = make_api("user")
    track_id = _track_in_library(engine)
    api.request("PUT", f"/favorites/{track_id}")

    api.request("DELETE", f"/favorites/{track_id}")

    with Session(engine) as session:
        dl = session.exec(select(Download).where(Download.track_id == track_id)).one()
        assert dl.status == ds.DOWNLOADED and dl.file_path == "A/In.flac"
    assert _intents(engine) == []
    assert dispatched_tasks[-1] == (
        "workers.maintenance.propagate_unlike",
        {"user_id": api.owner, "track_id": track_id, "sync_provider": "fakesync"},
    )


def test_favorites_are_per_user(make_api, engine):
    alice = make_api("user")
    track_id = _track_in_library(engine)
    alice.request("PUT", f"/favorites/{track_id}")
    admin = make_api("admin")

    mine = alice.request("GET", "/favorites")
    assert_conforms(mine, "GET", "/favorites")
    assert [t["library_id"] for t in mine.json()["items"]] == [track_id]
    assert mine.json()["items"][0]["favorite"] is True
    assert admin.request("GET", "/favorites").json()["items"] == []


# ---------------------------------------------------------------------------
# Collections: "download the missing ones" and "add the ones in library"
# ---------------------------------------------------------------------------


def test_add_to_favorites_likes_only_the_tracks_in_the_library(make_api, engine):
    api = make_api("user")
    downloaded = _track_in_library(engine, title="One")
    scanned = _track_in_library(engine, how="scan", title="Two")
    missing = _track_not_in_library(engine)
    downloading = _track_not_in_library(engine, status=ds.DOWNLOADING)

    resp = api.request("POST", "/favorites/batch", json={"track_ids": [downloaded, scanned, missing, downloading]})

    assert resp.status_code == 200
    assert_conforms(resp, "POST", "/favorites/batch")
    assert sorted(resp.json()["added"]) == sorted([downloaded, scanned])
    assert sorted(resp.json()["skipped"]) == sorted([missing, downloading])
    assert {f.track_id for f in _favorites(engine)} == {downloaded, scanned}
    # Again: nothing new.
    assert api.request("POST", "/favorites/batch", json={"track_ids": [downloaded]}).json()["added"] == []


def test_the_collection_summary_counts_what_the_heart_would_add(make_api, engine):
    from services.track_state import collection_summary

    tracks = [
        {"download_status": ds.DOWNLOADED, "in_library": True, "favorite": True},
        {"download_status": ds.DOWNLOADED, "in_library": True, "favorite": False},
        {"download_status": ds.DOWNLOADED, "in_library": True, "favorite": False},
        {"download_status": ds.FAILED, "in_library": False, "favorite": False},
        {"download_status": None, "in_library": False, "favorite": False},
    ]
    summary = collection_summary(tracks, total=6)

    assert summary["favorites"] == {"favorite": 1, "addable": 2}
    assert summary["library"] == {"total": 6, "downloaded": 3, "in_progress": 0, "failed": 1}
