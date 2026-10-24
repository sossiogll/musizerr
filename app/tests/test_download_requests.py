"""
The download approval policy, end to end (``services/download_requests.py``):
every mode of every kind, the threshold and the quota, the two levels, the
album delegation, what approval does and denial does not, the retry that
cannot bypass it, the likes job, open mode, who may do what, and what the API
tells the UI. Exact counts, on both database engines.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlmodel import Session, select

from core import download_policy as dp
from musizeer_sdk.models import Artist, Playlist
from db import download_request_store as store
from db.models import (
    Download,
    DownloadFollower,
    DownloadRequest,
    LibraryJobRun,
    TrackIdentifierRecord,
    TrackRecord,
    User,
    UserFavorite,
    UserSavedTrack,
)
from db.track_store import find_or_create_track
from routes.system.progress import event_visible_to
from services import download_requests as dr
from services import downloads

from tests.conftest import FakeAuthManager, FakeRegistry, make_album
from tests.download_requests_support import (
    Clock,
    Delegator,
    Harness,
    downloads_of,
    intents_of,
    requests_of,
    seed_in_library,
    seed_open_download,
    tracks_of,
)


@pytest.fixture
def h(engine, monkeypatch):
    return Harness(engine, monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(dr, "_now", clock)
    return clock


def _ask(h: Harness, who: str, spec, *, favorite: bool = True):
    """The service entry point, as the user ``who``."""
    with Session(h.engine) as session:
        user = session.get(User, h.id(who))
        return dr.request_download(
            session, user, spec, favorite_for=user.id if favorite else None, config=h.config, registry=h.registry,
            auth_manager=FakeAuthManager(),
        )


def _spec(kind: str, tag: str, count: int) -> dr.DownloadSpec:
    tracks = tracks_of(tag, 1 if kind == dp.KIND_TRACK else count)
    if kind == dp.KIND_TRACK:
        return dr.track_spec(tracks[0])
    if kind == dp.KIND_ALBUM:
        album = make_album(tag, "Artist", provider="catalog", provider_id=tag)
        album.tracks = tracks
        return dr.album_spec(album)
    if kind == dp.KIND_PLAYLIST:
        return dr.playlist_spec(Playlist(name=tag, provider="catalog", provider_id=tag, tracks=tracks))
    if kind == dp.KIND_ARTIST:
        return dr.artist_spec(Artist(name=tag, provider="catalog", provider_id=tag), tracks)
    return dr.liked_elsewhere_spec(tracks)


def _only(requests: list[DownloadRequest]) -> DownloadRequest:
    assert len(requests) == 1, [(r.kind, r.status) for r in requests]
    return requests[0]


# ---------------------------------------------------------------------------
# Every mode of every kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", dp.KINDS)
def test_auto_queues_at_once_and_is_recorded(h, kind):
    h.policy(**{f"{kind}_mode": dp.MODE_AUTO})
    count = 1 if kind == dp.KIND_TRACK else 6

    outcome = _ask(h, "user", _spec(kind, "auto", count))

    assert not outcome.pending
    request = _only(requests_of(h.engine))
    assert (request.kind, request.status, request.auto, request.reason) == (kind, "approved", True, dp.REASON_AUTO)
    assert (request.needed_tracks, request.queued_tracks, request.decided_by) == (count, count, None)
    assert request.decided_at is not None
    assert len(downloads_of(h.engine)) == count and len(h.queued) == count
    assert len(intents_of(h.engine)) == count


@pytest.mark.parametrize("kind", dp.KINDS)
def test_approval_queues_nothing_and_marks_no_favorite(h, kind):
    h.policy(**{f"{kind}_mode": dp.MODE_APPROVAL})
    count = 1 if kind == dp.KIND_TRACK else 6

    outcome = _ask(h, "user", _spec(kind, "wait", count))

    assert outcome.pending
    request = _only(requests_of(h.engine))
    assert (request.kind, request.status, request.auto, request.reason) == (kind, "pending", False, dp.REASON_KIND_REQUIRES_APPROVAL)
    assert (request.needed_tracks, request.queued_tracks, request.decided_at) == (count, None, None)
    assert downloads_of(h.engine) == [] and h.queued == [] and intents_of(h.engine) == []
    assert outcome.pending_body() == {
        "status": "pending_approval", "request_id": request.id, "kind": kind, "needed_tracks": count,
        "reason": dp.REASON_KIND_REQUIRES_APPROVAL,
    }


@pytest.mark.parametrize("kind", [k for k in dp.KINDS if k != dp.KIND_TRACK])
def test_auto_up_to_the_threshold_exactly_goes_and_one_more_waits(h, kind):
    h.policy(**{f"{kind}_mode": dp.MODE_AUTO_UP_TO, f"{kind}_threshold": 4})

    at = _ask(h, "user", _spec(kind, "at", 4))
    over = _ask(h, "user", _spec(kind, "over", 5))

    assert not at.pending and over.pending
    approved, waiting = requests_of(h.engine)
    assert (approved.status, approved.reason, approved.needed_tracks, approved.queued_tracks) == ("approved", dp.REASON_WITHIN_THRESHOLD, 4, 4)
    assert (waiting.status, waiting.reason, waiting.needed_tracks) == ("pending", dp.REASON_OVER_THRESHOLD, 5)
    assert len(downloads_of(h.engine)) == 4 and len(h.queued) == 4


def test_a_track_is_auto_up_to_its_one_track(h):
    h.policy(track_mode=dp.MODE_AUTO_UP_TO, track_threshold=1)

    assert not _ask(h, "user", _spec(dp.KIND_TRACK, "single", 1)).pending

    assert len(downloads_of(h.engine)) == 1


# ---------------------------------------------------------------------------
# What counts: only the tracks that need a download
# ---------------------------------------------------------------------------


def test_tracks_in_the_library_and_with_an_open_download_do_not_count(h):
    h.policy(playlist_mode=dp.MODE_AUTO_UP_TO, playlist_threshold=2)
    tracks = tracks_of("mixed", 6)
    seed_in_library(h.engine, tracks[:2])
    seed_open_download(h.engine, tracks[2:3], requested_by=h.id("other"))
    spec = dr.playlist_spec(Playlist(name="mixed", provider="catalog", provider_id="mixed", tracks=tracks))

    outcome = _ask(h, "user", spec)

    # Six tracks, two in the library, one already on its way: three need a download.
    assert outcome.pending
    request = _only(requests_of(h.engine))
    assert (request.status, request.reason, request.needed_tracks) == ("pending", dp.REASON_OVER_THRESHOLD, 3)


def test_the_same_playlist_is_approved_once_enough_of_it_is_there(h):
    h.policy(playlist_mode=dp.MODE_AUTO_UP_TO, playlist_threshold=3)
    tracks = tracks_of("mixed", 6)
    seed_in_library(h.engine, tracks[:2])
    seed_open_download(h.engine, tracks[2:3], requested_by=h.id("other"))
    spec = dr.playlist_spec(Playlist(name="mixed", provider="catalog", provider_id="mixed", tracks=tracks))

    outcome = _ask(h, "user", spec)

    assert not outcome.pending
    request = _only(requests_of(h.engine))
    assert (request.status, request.needed_tracks, request.queued_tracks) == ("approved", 3, 3)
    # Six tracks went through the gate: three queued, two in the library, one joined the open download.
    assert [r.status for r in outcome.execution.batch.results].count(downloads.QUEUED) == 3
    assert [r.status for r in outcome.execution.batch.results].count(downloads.ALREADY_IN_LIBRARY) == 2
    assert [r.status for r in outcome.execution.batch.results].count(downloads.ATTACHED) == 1
    assert len(h.queued) == 3
    with Session(h.engine) as session:
        assert len(session.exec(select(DownloadFollower)).all()) == 1


def test_a_failed_download_needs_a_new_one(h):
    h.policy(playlist_mode=dp.MODE_AUTO_UP_TO, playlist_threshold=1)
    tracks = tracks_of("again", 2)
    with Session(h.engine) as session:
        record = find_or_create_track(tracks[0], session)
        session.add(Download(track_id=record.internal_id, status="failed", requested_by=h.id("other")))
        session.commit()

    outcome = _ask(h, "user", dr.playlist_spec(Playlist(name="again", provider="catalog", provider_id="again", tracks=tracks)))

    assert outcome.pending
    assert _only(requests_of(h.engine)).needed_tracks == 2


def test_a_request_that_needs_nothing_is_not_recorded_and_goes_to_the_gate(h):
    h.policy(playlist_mode=dp.MODE_APPROVAL)
    tracks = tracks_of("owned", 3)
    seed_in_library(h.engine, tracks[:2])
    seed_open_download(h.engine, tracks[2:], requested_by=h.id("other"))

    outcome = _ask(h, "user", dr.playlist_spec(Playlist(name="owned", provider="catalog", provider_id="owned", tracks=tracks)))

    assert not outcome.pending and outcome.request is None
    assert requests_of(h.engine) == []
    assert sorted(r.status for r in outcome.execution.batch.results) == [
        downloads.ALREADY_IN_LIBRARY, downloads.ALREADY_IN_LIBRARY, downloads.ATTACHED,
    ]
    # The user still gets what costs nothing: their heart on the two tracks in the library, and the
    # favorite on arrival of the one whose download they joined.
    assert len(intents_of(h.engine)) == 1
    with Session(h.engine) as session:
        assert [f.user_id for f in session.exec(select(UserFavorite)).all()] == [h.id("user")] * 2
        assert len(session.exec(select(DownloadFollower)).all()) == 1
    assert h.queued == []


def test_asking_again_for_what_waits_finds_the_waiting_request(h):
    first = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "twice", 5))
    second = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "twice", 5))

    assert first.pending and second.pending
    assert second.request.id == first.request.id
    assert len(requests_of(h.engine)) == 1
    other = _ask(h, "other", _spec(dp.KIND_PLAYLIST, "twice", 5))
    assert other.request.id != first.request.id and len(requests_of(h.engine)) == 2


# ---------------------------------------------------------------------------
# The quota: a rolling window, from the approved requests
# ---------------------------------------------------------------------------


def test_the_quota_is_a_rolling_window(h, clock):
    h.policy(all_modes=dp.MODE_AUTO, quota=10, quota_days=7)

    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "w1", 6)).pending           # day 0: 6 of 10
    clock.advance(days=1)
    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "w2", 4)).pending           # day 1: 10 of 10
    clock.advance(days=1)
    over = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "w3", 1))                       # day 2: no room
    assert over.pending and over.request.reason == dp.REASON_OVER_QUOTA

    clock.advance(days=5)   # day 7: the first request is exactly at the edge of the window
    still = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "w4", 1))
    assert still.pending and still.request.reason == dp.REASON_OVER_QUOTA

    clock.advance(seconds=1)  # one second later it is out: 4 of 10 used
    fits = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "w5", 6))
    assert not fits.pending
    assert [(r.title, r.status) for r in requests_of(h.engine)] == [
        ("w1", "approved"), ("w2", "approved"), ("w3", "pending"), ("w4", "pending"), ("w5", "approved"),
    ]
    assert len(downloads_of(h.engine)) == 16


def test_only_approved_requests_count_towards_the_quota(h, clock):
    h.policy(all_modes=dp.MODE_AUTO, quota=10)
    waiting = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "big", 12))      # 12 > 10: waits, counts for nothing
    assert waiting.pending

    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "small", 10)).pending

    denied = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "denied", 11))
    assert denied.pending
    since = clock() - timedelta(days=7)
    with Session(h.engine) as session:
        assert store.tracks_in_window(session, h.id("user"), since) == 10
        dr.deny(session, denied.request.id, session.get(User, h.id("admin")), reason="no")
        dr.cancel(session, waiting.request.id, session.get(User, h.id("user")))
        assert store.tracks_in_window(session, h.id("user"), since) == 10
    assert sorted(r.status for r in requests_of(h.engine)) == ["approved", "cancelled", "denied"]


def test_the_quota_counts_what_was_queued_not_what_was_needed(h, clock):
    h.policy(all_modes=dp.MODE_AUTO, quota=10)
    _ask(h, "user", _spec(dp.KIND_PLAYLIST, "q1", 6))
    request = _only(requests_of(h.engine))
    assert request.queued_tracks == 6
    with Session(h.engine) as session:
        session.exec(update(DownloadRequest).values(queued_tracks=2))   # only two actually went in
        session.commit()

    fits = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "q2", 8))                       # 2 + 8 = 10: it fits

    assert not fits.pending


def test_the_quota_is_per_user(h, clock):
    h.policy(all_modes=dp.MODE_AUTO, quota=5)

    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "mine", 5)).pending
    assert _ask(h, "user", _spec(dp.KIND_PLAYLIST, "mine2", 1)).pending
    assert not _ask(h, "other", _spec(dp.KIND_PLAYLIST, "theirs", 5)).pending


def test_a_request_over_the_quota_waits_in_any_mode_with_the_reason_recorded(h):
    h.policy(playlist_mode=dp.MODE_AUTO, album_mode=dp.MODE_AUTO_UP_TO, album_threshold=100, quota=3)

    playlist = _ask(h, "user", _spec(dp.KIND_PLAYLIST, "p", 4))
    album = _ask(h, "user", _spec(dp.KIND_ALBUM, "a", 4))

    assert playlist.request.reason == album.request.reason == dp.REASON_OVER_QUOTA
    assert downloads_of(h.engine) == []


def test_no_quota_means_no_quota(h):
    h.policy(all_modes=dp.MODE_AUTO, quota=0)

    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "huge", 60)).pending

    assert len(downloads_of(h.engine)) == 60


# ---------------------------------------------------------------------------
# Who is governed
# ---------------------------------------------------------------------------


def test_an_admin_is_never_subject_to_the_policy(h):
    h.policy(all_modes=dp.MODE_APPROVAL, quota=1)

    outcome = _ask(h, "admin", _spec(dp.KIND_PLAYLIST, "adminlist", 12), favorite=False)

    assert not outcome.pending and outcome.request is None
    assert requests_of(h.engine) == []
    assert len(downloads_of(h.engine)) == 12 and len(h.queued) == 12
    assert {d.requested_by for d in downloads_of(h.engine)} == {h.id("admin")}


def test_open_mode_has_no_policy(engine, monkeypatch):
    h = Harness(engine, monkeypatch, auth=False)
    h.policy(all_modes=dp.MODE_APPROVAL, quota=1)

    resp = h.call("open", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-big-9"})

    assert resp.status_code == 200 and resp.json()["total_tracks"] == 9
    assert requests_of(engine) == [] and len(downloads_of(engine)) == 9
    assert {d.requested_by for d in downloads_of(engine)} == {None}
    status = h.call("open", "GET", "/auth/account/status", doc="/auth/account/status").json()
    assert status["mode"] == "open"
    assert status["download_policy"] == {"applies": False, "rules": None, "quota": None}


# ---------------------------------------------------------------------------
# Per-user overrides, set by an admin
# ---------------------------------------------------------------------------


def _override(h: Harness, who: str, body: dict):
    return h.call("admin", "PUT", f"/admin/users/{h.id(who)}/download-policy", json=body, doc="/admin/users/{user_id}/download-policy")


def test_an_override_changes_one_user_and_nobody_else(h):
    h.policy(album_mode=dp.MODE_AUTO_UP_TO, album_threshold=30)
    assert _override(h, "user", {"rules": {"album": {"mode": "approval"}}}).status_code == 200

    mine = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-a-2"})
    theirs = h.call("other", "POST", "/download/album", json={"provider": "catalog", "id": "album-b-2"})

    assert mine.status_code == 202 and mine.json()["reason"] == dp.REASON_KIND_REQUIRES_APPROVAL
    assert theirs.status_code == 200 and theirs.json()["total_tracks"] == 2
    assert [(r.user_id, r.status) for r in requests_of(h.engine)] == [(h.id("user"), "pending"), (h.id("other"), "approved")]


def test_the_override_wins_over_the_general_threshold_and_a_threshold_alone_keeps_the_mode(h):
    h.policy(album_mode=dp.MODE_AUTO_UP_TO, album_threshold=30)
    _override(h, "user", {"rules": {"album": {"threshold": 3}}})

    at = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-at-3"})
    over = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-over-4"})
    general = h.call("other", "POST", "/download/album", json={"provider": "catalog", "id": "album-gen-20"})

    assert (at.status_code, over.status_code, general.status_code) == (200, 202, 200)
    assert over.json()["reason"] == dp.REASON_OVER_THRESHOLD and over.json()["needed_tracks"] == 4


def test_a_quota_override_of_zero_lifts_the_quota_for_that_user_only(h):
    h.policy(all_modes=dp.MODE_AUTO, quota=5)
    _override(h, "user", {"quota_tracks": 0})

    free = h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-free-20"})
    limited = h.call("other", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-lim-20"})

    assert (free.status_code, limited.status_code) == (200, 202)
    assert limited.json()["reason"] == dp.REASON_OVER_QUOTA


def test_a_tighter_quota_override_binds_the_user(h):
    h.policy(all_modes=dp.MODE_AUTO, quota=100)
    _override(h, "user", {"quota_tracks": 3})

    assert h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-x-4"}).status_code == 202


def test_the_override_is_read_back_with_the_effective_policy_and_can_be_cleared(h):
    h.policy(playlist_mode=dp.MODE_APPROVAL, quota=50)
    body = {"rules": {"playlist": {"mode": "auto_up_to", "threshold": 7}}, "quota_tracks": 9}
    put = _override(h, "user", body)

    assert put.status_code == 200
    assert put.json()["override"] == {"rules": {"playlist": {"mode": "auto_up_to", "threshold": 7}}, "quota_tracks": 9}
    assert put.json()["effective"]["rules"]["playlist"] == {"mode": "auto_up_to", "threshold": 7}
    assert put.json()["effective"]["quota"] == {"tracks": 9, "window_days": 7}
    got = h.call("admin", "GET", f"/admin/users/{h.id('user')}/download-policy", doc="/admin/users/{user_id}/download-policy")
    assert got.json() == put.json()

    cleared = h.call("admin", "DELETE", f"/admin/users/{h.id('user')}/download-policy", doc="/admin/users/{user_id}/download-policy")
    assert cleared.json()["override"] == {"rules": {}, "quota_tracks": None}
    assert cleared.json()["effective"]["rules"]["playlist"]["mode"] == "approval"
    again = _override(h, "user", body)
    emptied = _override(h, "user", {})
    assert again.status_code == 200 and emptied.json()["override"] == {"rules": {}, "quota_tracks": None}


@pytest.mark.parametrize("body", [
    {"rules": {"movie": {"mode": "auto"}}},
    {"rules": {"album": {"mode": "always"}}},
    {"rules": {"album": {"threshold": 0}}},
    {"quota_tracks": -1},
    {"unknown": 1},
])
def test_an_invalid_override_is_refused_and_stores_nothing(h, body):
    resp = h.call("admin", "PUT", f"/admin/users/{h.id('user')}/download-policy", json=body)

    assert resp.status_code == 422
    assert h.call("admin", "GET", f"/admin/users/{h.id('user')}/download-policy").json()["override"] == {"rules": {}, "quota_tracks": None}


def test_an_override_for_a_user_that_does_not_exist_is_404(h):
    assert h.call("admin", "PUT", "/admin/users/nobody/download-policy", json={}).status_code == 404
    assert h.call("admin", "GET", "/admin/users/nobody/download-policy").status_code == 404


# ---------------------------------------------------------------------------
# The album delegated to an external system
# ---------------------------------------------------------------------------


@pytest.fixture
def hd(engine, monkeypatch):
    return Harness(engine, monkeypatch, delegator=Delegator())


def test_a_waiting_album_is_not_delegated_until_it_is_approved(hd):
    hd.policy(album_mode=dp.MODE_APPROVAL)

    resp = hd.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-del-5"})

    assert resp.status_code == 202
    assert hd.delegator.dispatched == []
    assert downloads_of(hd.engine) == [] and hd.queued == []
    request_id = resp.json()["request_id"]

    approved = hd.decide("admin", "approve", request_id)

    assert approved["outcome"] == "decided"
    assert hd.delegator.dispatched == ["album-del-5"]
    request = _only(requests_of(hd.engine))
    assert (request.status, request.dispatched_to, request.queued_tracks, request.needed_tracks) == ("approved", "delegator", 5, 5)
    # The external system has the album: nothing went through the per-track gate.
    assert downloads_of(hd.engine) == [] and hd.queued == []


def test_an_album_approved_at_once_is_delegated_at_once_and_counts_against_the_quota(hd):
    hd.policy(album_mode=dp.MODE_AUTO_UP_TO, album_threshold=30)

    resp = hd.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-go-4"}, doc="/download/album")

    assert resp.status_code == 200
    assert resp.json()["dispatched_to"] == "delegator" and resp.json()["download_ids"] == [] and resp.json()["total_tracks"] == 4
    assert resp.json()["details"] == [{"album": "album-go-4", "status": "queued"}]
    assert hd.delegator.dispatched == ["album-go-4"]
    request = _only(requests_of(hd.engine))
    assert (request.status, request.auto, request.dispatched_to, request.queued_tracks) == ("approved", True, "delegator", 4)


def test_an_admins_album_is_delegated_without_a_record(hd):
    hd.policy(album_mode=dp.MODE_APPROVAL)

    resp = hd.call("admin", "POST", "/download/album", json={"provider": "catalog", "id": "album-mine-3"}, doc="/download/album")

    assert resp.status_code == 200 and resp.json()["dispatched_to"] == "delegator"
    assert hd.delegator.dispatched == ["album-mine-3"] and requests_of(hd.engine) == []


def test_an_album_the_external_system_declines_goes_track_by_track_on_approval(engine, monkeypatch):
    h = Harness(engine, monkeypatch, delegator=Delegator(delegates=False))
    h.policy(album_mode=dp.MODE_APPROVAL)
    request_id = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-tbt-3"}).json()["request_id"]

    h.decide("admin", "approve", request_id)

    assert h.delegator.dispatched == ["album-tbt-3"]            # it was asked, once, and passed
    assert len(downloads_of(engine)) == 3 and len(h.queued) == 3


# ---------------------------------------------------------------------------
# Approval, denial, cancellation
# ---------------------------------------------------------------------------


def _waiting_playlist(h: Harness, who: str = "user", tag: str = "list", count: int = 3) -> str:
    resp = h.call(who, "POST", "/download/playlist", json={"provider": "catalog", "id": f"playlist-{tag}-{count}"})
    assert resp.status_code == 202, resp.text
    return resp.json()["request_id"]


def test_approval_queues_through_the_gate_once_and_marks_the_favorites_only_then(h):
    request_id = _waiting_playlist(h)
    assert intents_of(h.engine) == [] and downloads_of(h.engine) == [] and h.queued == []

    first = h.decide("admin", "approve", request_id, note="fine")
    second = h.decide("admin2", "approve", request_id)

    assert (first["outcome"], second["outcome"]) == ("decided", "unchanged")
    assert len(downloads_of(h.engine)) == 3 and len(h.queued) == 3
    assert {d.requested_by for d in downloads_of(h.engine)} == {h.id("user")}
    assert sorted(i.user_id for i in intents_of(h.engine)) == [h.id("user")] * 3
    request = _only(requests_of(h.engine))
    assert (request.status, request.auto, request.decided_by, request.decision_note) == ("approved", False, h.id("admin"), "fine")
    assert (request.queued_tracks, request.reason) == (3, dp.REASON_KIND_REQUIRES_APPROVAL)
    admin_view = first["request"]
    assert admin_view["requested_by"]["username"] == "user" and admin_view["decided_by"]["username"] == "admin"


def test_a_denied_request_queues_nothing_and_leaves_no_favorite(h):
    request_id = _waiting_playlist(h)

    resp = h.decide("admin", "deny", request_id, reason="disk")
    again = h.decide("admin2", "deny", request_id)

    assert (resp["outcome"], again["outcome"]) == ("decided", "unchanged")
    assert downloads_of(h.engine) == [] and h.queued == [] and intents_of(h.engine) == []
    request = _only(requests_of(h.engine))
    assert (request.status, request.decision_note, request.decided_by, request.queued_tracks) == ("denied", "disk", h.id("admin"), None)
    approve = h.decide("admin", "approve", request_id)
    assert approve["outcome"] == "conflict" and approve["request"]["status"] == "denied"
    assert downloads_of(h.engine) == [] and intents_of(h.engine) == []


def test_a_cancelled_request_queues_nothing_and_leaves_no_favorite(h):
    request_id = _waiting_playlist(h)

    resp = h.call("user", "POST", f"/download-requests/{request_id}/cancel", doc="/download-requests/{request_id}/cancel")
    again = h.call("user", "POST", f"/download-requests/{request_id}/cancel", doc="/download-requests/{request_id}/cancel")

    assert resp.status_code == 200 and resp.json()["changed"] is True and again.json()["changed"] is False
    assert resp.json()["request"]["status"] == "cancelled" and "requested_by" not in resp.json()["request"]
    assert downloads_of(h.engine) == [] and h.queued == [] and intents_of(h.engine) == []
    approve = h.decide("admin", "approve", request_id)
    deny = h.decide("admin", "deny", request_id)
    assert (approve["outcome"], deny["outcome"]) == ("conflict", "conflict")
    assert downloads_of(h.engine) == []
    assert _only(requests_of(h.engine)).decided_by == h.id("user")


def test_a_user_cannot_cancel_what_an_admin_already_decided(h):
    request_id = _waiting_playlist(h)
    h.decide("admin", "approve", request_id)

    resp = h.call("user", "POST", f"/download-requests/{request_id}/cancel", doc="/download-requests/{request_id}/cancel")

    assert resp.status_code == 409
    assert _only(requests_of(h.engine)).status == "approved" and len(downloads_of(h.engine)) == 3


def test_deciding_a_request_that_does_not_exist_is_not_found(h):
    assert h.decide("admin", "approve", "nope") == {"id": "nope", "outcome": "not_found", "request": None}
    assert h.decide("admin", "deny", "nope") == {"id": "nope", "outcome": "not_found", "request": None}
    assert h.call("user", "POST", "/download-requests/nope/cancel").status_code == 404


def test_the_tracks_that_came_in_meanwhile_are_not_downloaded_again_on_approval(h):
    request_id = _waiting_playlist(h, count=4)
    seed_in_library(h.engine, tracks_of("playlist-list-4", 4)[:1])

    h.decide("admin", "approve", request_id)

    request = _only(requests_of(h.engine))
    assert request.needed_tracks == 4 and request.queued_tracks == 3
    assert len(h.queued) == 3
    # The heart still lands on the one that was already there.
    assert len(intents_of(h.engine)) == 3
    with Session(h.engine) as session:
        assert len(session.exec(select(UserFavorite)).all()) == 1


def _broken(*args, **kwargs):
    raise RuntimeError("database down")


def test_approval_that_fails_before_queueing_anything_goes_back_to_pending(h, monkeypatch):
    request_id = _waiting_playlist(h)
    real = dr.enqueue_tracks
    monkeypatch.setattr(dr, "enqueue_tracks", _broken)

    with Session(h.engine) as session:
        with pytest.raises(RuntimeError, match="database down"):
            dr.approve(session, request_id, session.get(User, h.id("admin")), note=None, config=h.config,
                       registry=h.registry, auth_manager=FakeAuthManager())

    request = _only(requests_of(h.engine))
    assert (request.status, request.decided_by, request.decided_at, request.queued_tracks) == ("pending", None, None, None)
    assert downloads_of(h.engine) == [] and intents_of(h.engine) == []
    monkeypatch.setattr(dr, "enqueue_tracks", real)
    ok = h.decide("admin", "approve", request_id)
    assert ok["outcome"] == "decided" and len(h.queued) == 3 and _only(requests_of(h.engine)).status == "approved"


def test_an_auto_approved_request_that_fails_before_queueing_leaves_no_record(h, monkeypatch):
    h.policy(playlist_mode=dp.MODE_AUTO)
    monkeypatch.setattr(dr, "enqueue_tracks", _broken)

    with pytest.raises(RuntimeError, match="database down"):
        _ask(h, "user", _spec(dp.KIND_PLAYLIST, "fail", 3))

    assert requests_of(h.engine) == []


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def test_a_batch_decides_each_request_once_and_reports_each_outcome(h):
    first, second, third = (_waiting_playlist(h, tag=f"b{i}", count=2) for i in range(3))
    h.decide("admin", "deny", third)

    resp = h.call("admin", "POST", "/admin/download-requests/approve", doc="/admin/download-requests/approve",
                  json={"ids": [first, second, third, "missing", first], "note": "all"})

    assert resp.status_code == 200
    assert [(item["id"], item["outcome"]) for item in resp.json()["results"]] == [
        (first, "decided"), (second, "decided"), (third, "conflict"), ("missing", "not_found"),
    ]
    assert resp.json()["results"][3]["request"] is None and resp.json()["results"][0]["request"]["decision_note"] == "all"
    assert len(downloads_of(h.engine)) == 4 and len(h.queued) == 4
    again = h.call("admin", "POST", "/admin/download-requests/approve", json={"ids": [first]})
    assert again.json()["results"][0]["outcome"] == "unchanged" and len(h.queued) == 4


def test_a_batch_deny(h):
    ids = [_waiting_playlist(h, tag=f"d{i}", count=2) for i in range(2)]

    resp = h.call("admin", "POST", "/admin/download-requests/deny", doc="/admin/download-requests/deny",
                  json={"ids": ids, "reason": "not now"})

    assert [item["outcome"] for item in resp.json()["results"]] == ["decided", "decided"]
    assert [(r.status, r.decision_note) for r in requests_of(h.engine)] == [("denied", "not now")] * 2
    assert downloads_of(h.engine) == [] and intents_of(h.engine) == []


def test_a_batch_where_one_request_fails_goes_on_and_reports_it(h, monkeypatch):
    bad = _waiting_playlist(h, tag="bad", count=2)
    good = _waiting_playlist(h, tag="good", count=2)
    real = dr.enqueue_tracks

    def selective(tracks, *args, **kwargs):
        if tracks and tracks[0].album == "playlist-bad-2":
            raise RuntimeError("boom")
        return real(tracks, *args, **kwargs)

    monkeypatch.setattr(dr, "enqueue_tracks", selective)

    resp = h.call("admin", "POST", "/admin/download-requests/approve", json={"ids": [bad, good]})

    assert [item["outcome"] for item in resp.json()["results"]] == ["failed", "decided"]
    assert sorted(r.status for r in requests_of(h.engine)) == ["approved", "pending"]
    assert len(downloads_of(h.engine)) == 2


@pytest.mark.parametrize("body", [{"ids": []}, {"ids": ["x"] * 101}, {}])
def test_a_batch_needs_between_one_and_a_hundred_ids(h, body):
    assert h.call("admin", "POST", "/admin/download-requests/approve", json=body).status_code == 422


# ---------------------------------------------------------------------------
# A retry is not a request: it cannot start a download nobody approved
# ---------------------------------------------------------------------------


def _record_of(engine, track) -> str:
    with Session(engine) as session:
        return find_or_create_track(track, session).internal_id


def test_a_retry_of_a_track_that_never_had_an_approved_download_is_refused(h):
    track_id = _record_of(h.engine, tracks_of("never", 1)[0])

    resp = h.call("user", "POST", f"/downloads/{track_id}/retry", doc="/downloads/{track_id}/retry")

    assert resp.status_code == 403 and resp.json()["detail"]["error_code"] == "retry_not_approved"
    assert downloads_of(h.engine) == [] and h.queued == [] and intents_of(h.engine) == []


def test_a_retry_after_a_denial_is_refused_too(h):
    request_id = _waiting_playlist(h, count=2)
    h.decide("admin", "deny", request_id)
    track_id = _record_of(h.engine, tracks_of("playlist-list-2", 2)[0])

    assert h.call("user", "POST", f"/downloads/{track_id}/retry").status_code == 403
    assert h.call("user", "POST", "/downloads/retry-failed").json() == {"download_ids": []}
    assert downloads_of(h.engine) == []


def test_a_retry_of_somebody_elses_download_is_refused(h):
    track = tracks_of("theirs", 1)[0]
    track_id = _record_of(h.engine, track)
    with Session(h.engine) as session:
        session.add(Download(track_id=track_id, status="failed", requested_by=h.id("other")))
        session.commit()

    assert h.call("user", "POST", f"/downloads/{track_id}/retry").status_code == 403
    assert len(downloads_of(h.engine)) == 1 and h.queued == []


def test_a_retry_of_a_download_the_user_has_is_not_governed(h):
    track_id = _record_of(h.engine, tracks_of("mine", 1)[0])
    follower_track_id = _record_of(h.engine, tracks_of("joined", 1)[0])
    with Session(h.engine) as session:
        session.add(Download(track_id=track_id, status="failed", requested_by=h.id("user")))
        other = Download(track_id=follower_track_id, status="failed", requested_by=h.id("other"))
        session.add(other)
        session.commit()
        session.add(DownloadFollower(download_id=other.id, user_id=h.id("user")))
        session.commit()
    h.policy(all_modes=dp.MODE_APPROVAL, quota=1)

    own = h.call("user", "POST", f"/downloads/{track_id}/retry")
    joined = h.call("user", "POST", f"/downloads/{follower_track_id}/retry")
    bulk = h.call("other", "POST", "/downloads/retry-failed")

    assert own.status_code == 200 and own.json()["status"] == downloads.QUEUED
    assert joined.status_code == 200 and joined.json()["status"] == downloads.QUEUED
    assert bulk.status_code == 200 and bulk.json() == {"download_ids": []}   # nothing of theirs is still failed
    assert len(h.queued) == 2
    assert requests_of(h.engine) == []


def test_an_admin_and_open_mode_may_retry_any_track(engine, monkeypatch):
    h = Harness(engine, monkeypatch)
    track_id = _record_of(engine, tracks_of("any", 1)[0])

    assert h.call("admin", "POST", f"/downloads/{track_id}/retry").status_code == 200

    h_open = Harness(engine, monkeypatch, auth=False)
    other_id = _record_of(engine, tracks_of("any2", 1)[0])
    assert h_open.call("open", "POST", f"/downloads/{other_id}/retry").status_code == 200


def test_the_transient_failure_routine_retries_without_asking_the_policy(h):
    track_id = _record_of(h.engine, tracks_of("routine", 1)[0])
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with Session(h.engine) as session:
        session.add(Download(track_id=track_id, status="failed", requested_by=h.id("user"), retriable=True,
                             started_at=long_ago, completed_at=long_ago))
        session.commit()
    h.policy(all_modes=dp.MODE_APPROVAL, quota=1)

    with Session(h.engine) as session:
        result = downloads.retry_transient_failures(session, h.registry, h.config)

    assert result == {"candidates": 1, "queued": 1}
    assert len(h.queued) == 1 and requests_of(h.engine) == []


# ---------------------------------------------------------------------------
# The likes download job is a request too
# ---------------------------------------------------------------------------


def _likes(engine, user_id: str, count: int, tag: str = "liked") -> None:
    with Session(engine) as session:
        for i in range(count):
            record = TrackRecord(
                title=f"{tag} {i}", artist="A", album="B",
                track_data=json.dumps({"title": f"{tag} {i}", "artist": "A", "album": "B", "duration_ms": 1}),
            )
            session.add(record)
            session.flush()
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="isrc", value=f"ISRC-{tag}-{i}"))
            session.add(UserSavedTrack(user_id=user_id, track_id=record.internal_id))
        session.commit()


def _run_likes_job(h: Harness, who: str, monkeypatch) -> dict:
    from db import library_job_store as job_store
    from workers import tasks

    user_id = h.id(who)
    monkeypatch.setattr(tasks, "engine", h.engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: h.registry)
    monkeypatch.setattr(tasks, "get_config", lambda: h.config)
    with Session(h.engine) as session:
        run = job_store.create_run(session, user_id, "download_sync", params={"time_range": "all"})
    tasks.library_download_sync.run(user_id=user_id, time_range="all", run_id=run.id)
    with Session(h.engine) as session:
        done = session.get(LibraryJobRun, run.id)
        assert done.status == "completed", done.error_message
        return json.loads(done.result)


def test_the_likes_download_of_a_user_over_the_threshold_waits_for_approval(h, monkeypatch):
    h.policy(liked_elsewhere_mode=dp.MODE_AUTO_UP_TO, liked_elsewhere_threshold=3)
    _likes(h.engine, h.id("user"), 5)

    result = _run_likes_job(h, "user", monkeypatch)

    request = _only(requests_of(h.engine))
    assert (request.kind, request.status, request.reason, request.needed_tracks) == ("liked_elsewhere", "pending", dp.REASON_OVER_THRESHOLD, 5)
    assert result == {
        "total": 5, "checked": 5, "queued": 0, "errors": 0, "skipped_removed": 0,
        "pending_approval": {"request_id": request.id, "needed_tracks": 5, "reason": dp.REASON_OVER_THRESHOLD},
    }
    assert downloads_of(h.engine) == [] and h.queued == [] and intents_of(h.engine) == []

    # Running it again while the request waits finds that same request.
    again = _run_likes_job(h, "user", monkeypatch)
    assert again["pending_approval"]["request_id"] == request.id and len(requests_of(h.engine)) == 1

    assert h.decide("admin", "approve", request.id)["outcome"] == "decided"
    assert len(downloads_of(h.engine)) == 5 and len(h.queued) == 5
    assert sorted(i.user_id for i in intents_of(h.engine)) == [h.id("user")] * 5


def test_the_likes_download_within_the_threshold_goes_and_is_recorded(h, monkeypatch):
    h.policy(liked_elsewhere_mode=dp.MODE_AUTO_UP_TO, liked_elsewhere_threshold=3)
    _likes(h.engine, h.id("user"), 3)

    result = _run_likes_job(h, "user", monkeypatch)

    assert (result["queued"], result["errors"]) == (3, 0) and "pending_approval" not in result
    request = _only(requests_of(h.engine))
    assert (request.kind, request.status, request.auto, request.queued_tracks) == ("liked_elsewhere", "approved", True, 3)
    assert len(downloads_of(h.engine)) == 3 and len(intents_of(h.engine)) == 3


def test_the_likes_download_of_an_admin_is_not_governed(h, monkeypatch):
    h.policy(liked_elsewhere_mode=dp.MODE_APPROVAL, quota=1)
    _likes(h.engine, h.id("admin"), 4)

    result = _run_likes_job(h, "admin", monkeypatch)

    assert result["queued"] == 4 and requests_of(h.engine) == []
    assert len(downloads_of(h.engine)) == 4


def test_the_likes_download_counts_only_the_likes_that_need_a_download(h, monkeypatch):
    h.policy(liked_elsewhere_mode=dp.MODE_AUTO_UP_TO, liked_elsewhere_threshold=2)
    _likes(h.engine, h.id("user"), 5)
    with Session(h.engine) as session:
        # Three of the five are already on their way.
        for i in range(3):
            row = session.exec(select(TrackIdentifierRecord).where(TrackIdentifierRecord.value == f"ISRC-liked-{i}")).one()
            session.add(Download(track_id=row.track_id, status="pending", requested_by=h.id("other")))
        session.commit()

    result = _run_likes_job(h, "user", monkeypatch)

    assert result["queued"] == 2 and "pending_approval" not in result
    assert _only(requests_of(h.engine)).needed_tracks == 2


# ---------------------------------------------------------------------------
# The routes: shapes, defaults, listing
# ---------------------------------------------------------------------------


def test_the_defaults_let_a_track_and_a_normal_album_go_and_hold_the_big_requests(h):
    assert h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "track-one"}, doc="/download/track").status_code == 200
    assert h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-ok-30"}, doc="/download/album").status_code == 200
    big = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-box-31"}, doc="/download/album")
    playlist = h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-p-2"}, doc="/download/playlist")
    artist = h.call("user", "POST", "/download/artist", json={"provider": "catalog", "id": "artist-a-2"}, doc="/download/artist")

    assert [r.status_code for r in (big, playlist, artist)] == [202, 202, 202]
    assert big.json() == {
        "status": "pending_approval", "request_id": big.json()["request_id"], "kind": "album", "needed_tracks": 31,
        "reason": "over_threshold",
    }
    assert (playlist.json()["kind"], playlist.json()["reason"], playlist.json()["needed_tracks"]) == ("playlist", "kind_requires_approval", 2)
    assert (artist.json()["kind"], artist.json()["reason"]) == ("artist", "kind_requires_approval")
    assert [(r.kind, r.status) for r in requests_of(h.engine)] == [
        ("track", "approved"), ("album", "approved"), ("album", "pending"), ("playlist", "pending"), ("artist", "pending"),
    ]
    assert len(downloads_of(h.engine)) == 31


def test_an_approved_request_keeps_the_response_of_a_download(h):
    h.policy(all_modes=dp.MODE_AUTO)

    track = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "track-t"}).json()
    playlist = h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-pl-3"}).json()

    assert track["status"] == "queued" and track["download_id"] and track["track"]["favorite_pending"] is True
    assert (len(playlist["download_ids"]), playlist["total_tracks"], playlist["skipped"]) == (3, 3, 0)


def test_the_requests_of_a_user_are_listed_for_that_user_only(h):
    mine = _waiting_playlist(h, "user", "mine", 2)
    theirs = _waiting_playlist(h, "other", "theirs", 2)
    h.call("user", "POST", f"/download-requests/{mine}/cancel")

    everything = h.call("user", "GET", "/download-requests", doc="/download-requests").json()
    pending = h.call("user", "GET", "/download-requests?status=pending", doc="/download-requests").json()
    other = h.call("other", "GET", "/download-requests", doc="/download-requests").json()

    assert [r["id"] for r in everything["items"]] == [mine] and everything["total"] == 1
    assert pending["items"] == [] and pending["total"] == 0
    assert [r["id"] for r in other["items"]] == [theirs]
    assert "requested_by" not in everything["items"][0] and "decided_by" not in everything["items"][0]
    assert h.call("user", "POST", f"/download-requests/{theirs}/cancel").status_code == 404
    assert _only([r for r in requests_of(h.engine) if r.id == theirs]).status == "pending"


def test_the_admin_lists_filters_and_counts(h):
    first = _waiting_playlist(h, "user", "a", 2)
    _waiting_playlist(h, "other", "b", 2)
    third = h.call("user", "POST", "/download/artist", json={"provider": "catalog", "id": "artist-z-2"}).json()["request_id"]
    h.decide("admin", "deny", first)

    everything = h.call("admin", "GET", "/admin/download-requests", doc="/admin/download-requests").json()
    pending = h.call("admin", "GET", "/admin/download-requests?status=pending", doc="/admin/download-requests").json()
    artists = h.call("admin", "GET", "/admin/download-requests?kind=artist", doc="/admin/download-requests").json()
    users = h.call("admin", "GET", f"/admin/download-requests?user_id={h.id('other')}", doc="/admin/download-requests").json()
    both = h.call("admin", "GET", "/admin/download-requests?status=pending&status=denied&limit=2&page=2",
                  doc="/admin/download-requests").json()
    count = h.call("admin", "GET", "/admin/download-requests/pending-count", doc="/admin/download-requests/pending-count")

    assert everything["total"] == 3 and {i["requested_by"]["username"] for i in everything["items"]} == {"user", "other"}
    assert pending["total"] == 2 and artists["total"] == 1 and artists["items"][0]["id"] == third
    assert users["total"] == 1 and users["items"][0]["requested_by"]["username"] == "other"
    assert (both["total"], len(both["items"]), both["pages"]) == (3, 1, 2)
    assert count.json() == {"pending": 2}
    denied = next(i for i in everything["items"] if i["id"] == first)
    assert denied["decided_by"] == {"id": h.id("admin"), "username": "admin"}


def test_the_general_policy_is_read_and_changed_through_the_settings(h):
    got = h.call("admin", "GET", "/admin/download-policy", doc="/admin/download-policy")

    assert got.json()["rules"]["album"] == {"mode": "auto_up_to", "threshold": 30}
    assert got.json()["rules"]["playlist"]["mode"] == "approval"
    assert got.json()["quota"] == {"tracks": 200, "window_days": 7}

    patch = h.call("admin", "PATCH", "/admin/download-policy", doc="/admin/download-policy", json={
        "rules": {"playlist": {"mode": "auto_up_to", "threshold": 12}}, "quota": {"tracks": 40},
    })

    assert patch.status_code == 200
    assert patch.json()["rules"]["playlist"] == {"mode": "auto_up_to", "threshold": 12}
    assert patch.json()["quota"] == {"tracks": 40, "window_days": 7}
    assert h.config.get("download_request_playlist_threshold") == 12 and h.config.get("download_request_quota_tracks") == 40
    assert h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-x-12"}).status_code == 200
    assert h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-y-13"}).status_code == 202


@pytest.mark.parametrize("body", [
    {"rules": {"playlist": {"mode": "sometimes"}}},
    {"rules": {"playlist": {"threshold": 0}}},
    {"rules": {"movie": {"mode": "auto"}}},
    {"quota": {"tracks": -5}},
    {"quota": {"window_days": 0}},
    {"surprise": True},
])
def test_an_invalid_general_policy_is_refused_and_changes_nothing(h, body):
    resp = h.call("admin", "PATCH", "/admin/download-policy", json=body)

    assert resp.status_code == 422
    assert h.call("admin", "GET", "/admin/download-policy").json()["rules"]["playlist"]["mode"] == "approval"


def test_the_policy_settings_are_in_the_catalogue_for_admins_only(h):
    assert h.config.get("download_request_album_mode") == "auto_up_to"
    assert h.config.get("download_request_quota_days") == 7


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------

_ADMIN_ROUTES = [
    ("GET", "/admin/download-requests", None),
    ("GET", "/admin/download-requests/pending-count", None),
    ("POST", "/admin/download-requests/approve", {"ids": ["x"]}),
    ("POST", "/admin/download-requests/deny", {"ids": ["x"]}),
    ("GET", "/admin/download-policy", None),
    ("PATCH", "/admin/download-policy", {"quota": {"tracks": 1}}),
    ("GET", "/admin/users/{uid}/download-policy", None),
    ("PUT", "/admin/users/{uid}/download-policy", {}),
    ("DELETE", "/admin/users/{uid}/download-policy", None),
]


@pytest.mark.parametrize("method, url, body", _ADMIN_ROUTES)
def test_a_user_cannot_use_an_admin_route_and_a_guest_is_not_signed_in(h, method, url, body):
    _waiting_playlist(h)
    url = url.replace("{uid}", h.id("user"))
    kwargs = {} if body is None else {"json": body}

    assert h.call("user", method, url, **kwargs).status_code == 403
    assert h.call("guest", method, url, **kwargs).status_code == 401
    assert _only(requests_of(h.engine)).status == "pending" and downloads_of(h.engine) == []


def test_a_user_cannot_approve_their_own_request_or_change_their_own_policy(h):
    request_id = _waiting_playlist(h)

    approve = h.call("user", "POST", "/admin/download-requests/approve", json={"ids": [request_id]})
    policy = h.call("user", "PATCH", "/admin/download-policy", json={"quota": {"tracks": 0}, "rules": {"playlist": {"mode": "auto"}}})
    own = h.call("user", "PUT", f"/admin/users/{h.id('user')}/download-policy", json={"quota_tracks": 0})

    assert (approve.status_code, policy.status_code, own.status_code) == (403, 403, 403)
    assert h.config.get("download_request_quota_tracks") == 200
    assert h.call("admin", "GET", f"/admin/users/{h.id('user')}/download-policy").json()["override"]["quota_tracks"] is None


def test_the_policy_is_not_a_personal_setting(h):
    from core.settings_catalog import get_def

    assert all(get_def(key).scope == "system" for key in (
        dp.QUOTA_TRACKS_KEY, dp.QUOTA_DAYS_KEY, *(dp.mode_key(k) for k in dp.KINDS), *(dp.threshold_key(k) for k in dp.KINDS),
    ))


def test_the_requests_routes_need_a_signed_in_user(h):
    assert h.call("guest", "GET", "/download-requests").status_code == 401
    assert h.call("guest", "POST", "/download-requests/x/cancel").status_code == 401


# ---------------------------------------------------------------------------
# What the UI is told
# ---------------------------------------------------------------------------


def test_the_status_tells_a_user_what_a_request_would_meet_right_now(h, clock):
    h.policy(quota=10, playlist_mode=dp.MODE_AUTO)
    assert not _ask(h, "user", _spec(dp.KIND_PLAYLIST, "used", 6)).pending
    h.policy(playlist_mode=dp.MODE_APPROVAL)

    status = h.call("user", "GET", "/auth/account/status", doc="/auth/account/status").json()

    policy = status["download_policy"]
    assert policy["applies"] is True
    assert policy["quota"] == {"tracks": 10, "window_days": 7, "used": 6, "remaining": 4}
    assert policy["rules"]["track"] == {"mode": "auto", "threshold": 1, "auto_up_to": 4}
    assert policy["rules"]["album"] == {"mode": "auto_up_to", "threshold": 30, "auto_up_to": 4}
    assert policy["rules"]["playlist"]["auto_up_to"] == 0 and policy["rules"]["artist"]["auto_up_to"] == 0
    assert policy["rules"]["liked_elsewhere"]["auto_up_to"] == 4


def test_the_status_without_a_quota_leaves_the_automatic_kinds_unlimited(h):
    h.policy(quota=0, track_mode=dp.MODE_AUTO)

    rules = h.call("user", "GET", "/auth/account/status").json()["download_policy"]

    assert rules["quota"] == {"tracks": 0, "window_days": 7, "used": 0, "remaining": None}
    assert rules["rules"]["track"]["auto_up_to"] is None and rules["rules"]["album"]["auto_up_to"] == 30


def test_the_status_follows_the_users_override(h):
    _override(h, "user", {"rules": {"album": {"mode": "approval"}}, "quota_tracks": 3})

    mine = h.call("user", "GET", "/auth/account/status").json()["download_policy"]
    theirs = h.call("other", "GET", "/auth/account/status").json()["download_policy"]

    assert mine["rules"]["album"]["auto_up_to"] == 0 and mine["quota"]["tracks"] == 3
    assert theirs["rules"]["album"]["auto_up_to"] == 30 and theirs["quota"]["tracks"] == 200


def test_the_status_says_the_policy_does_not_apply_to_an_admin_and_nothing_to_a_guest(h):
    admin = h.call("admin", "GET", "/auth/account/status", doc="/auth/account/status").json()
    guest = h.call("guest", "GET", "/auth/account/status", doc="/auth/account/status").json()

    assert admin["download_policy"] == {"applies": False, "rules": None, "quota": None}
    assert guest["download_policy"] is None


def test_what_the_status_promises_is_what_the_server_does(h, clock):
    h.policy(quota=10, album_threshold=8)

    def promised() -> int:
        return h.call("user", "GET", "/auth/account/status").json()["download_policy"]["rules"]["album"]["auto_up_to"]

    assert promised() == 8
    at = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-edge-8"})
    assert at.status_code == 200 and promised() == 2              # the quota left now bounds it
    over = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-over-3"})
    fits = h.call("user", "POST", "/download/album", json={"provider": "catalog", "id": "album-fits-2"})

    assert (over.status_code, over.json()["reason"], fits.status_code) == (202, "over_quota", 200)
    assert promised() == 0


# ---------------------------------------------------------------------------
# Notifications on the progress stream
# ---------------------------------------------------------------------------


def _visible(event: dict, user: User) -> bool:
    return event_visible_to(event, user, lambda download_id: False)


def test_a_waiting_request_tells_the_admins_and_only_them(h, request_events):
    request_id = _waiting_playlist(h)

    (event,) = request_events
    assert (event["type"], event["event"], event["request"]["id"], event["pending"]) == ("download_request", "pending", request_id, 1)
    assert "user_id" not in event and event["request"]["requested_by"]["username"] == "user"
    assert _visible(event, h.users["admin"]) and _visible(event, h.users["admin2"])
    assert not _visible(event, h.users["user"]) and not _visible(event, h.users["other"])


def test_a_decision_tells_the_requester_and_the_admins(h, request_events):
    approved = _waiting_playlist(h, tag="yes")
    denied = _waiting_playlist(h, tag="no")
    request_events.clear()

    h.decide("admin", "approve", approved)
    h.decide("admin", "deny", denied, reason="disk")
    h.decide("admin", "approve", approved)     # nothing changed: no event

    kinds = [(e["event"], e.get("user_id")) for e in request_events]
    assert kinds == [
        ("approved", h.id("user")), ("approved", None), ("denied", h.id("user")), ("denied", None),
    ]
    to_user = request_events[0]
    assert to_user["request"]["status"] == "approved" and "requested_by" not in to_user["request"]
    assert _visible(to_user, h.users["user"]) and not _visible(to_user, h.users["admin"]) and not _visible(to_user, h.users["other"])
    assert request_events[3]["pending"] == 0 and _visible(request_events[3], h.users["admin"])
    assert request_events[2]["request"]["decision_note"] == "disk"


def test_a_cancellation_tells_the_admins(h, request_events):
    request_id = _waiting_playlist(h)
    request_events.clear()

    h.call("user", "POST", f"/download-requests/{request_id}/cancel")

    (event,) = request_events
    assert event["event"] == "cancelled" and event["pending"] == 0 and "user_id" not in event
    assert _visible(event, h.users["admin"]) and not _visible(event, h.users["user"])


def test_an_approval_at_once_sends_no_event(h, request_events):
    h.policy(playlist_mode=dp.MODE_AUTO)

    _ask(h, "user", _spec(dp.KIND_PLAYLIST, "quiet", 2))

    assert request_events == []


def test_a_stream_that_is_down_never_breaks_a_request(h, monkeypatch):
    def down(event):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(dr, "publish_event", down)

    assert h.call("user", "POST", "/download/playlist", json={"provider": "catalog", "id": "playlist-q-2"}).status_code == 202


# ---------------------------------------------------------------------------
# What a request stores, and what the catalog does not
# ---------------------------------------------------------------------------


def test_a_waiting_request_keeps_what_approval_needs_and_writes_nothing_to_the_catalog(h):
    request_id = _waiting_playlist(h, count=3)

    request = _only(requests_of(h.engine))
    assert request.id == request_id
    assert (request.target_provider, request.target_id, request.title) == ("catalog", "playlist-list-3", "playlist-list-3")
    payload = json.loads(request.payload)
    assert [t["provider_id"] for t in payload["tracks"]] == ["playlist-list-3-0", "playlist-list-3-1", "playlist-list-3-2"]
    assert payload["album"] is None
    with Session(h.engine) as session:
        assert session.exec(select(TrackRecord)).all() == []   # a request nobody approves leaves nothing behind


def test_a_stored_cover_is_a_reference_and_comes_back_with_the_prefix(h, monkeypatch):
    monkeypatch.setenv("MUSIZEER_PUBLIC_API_PREFIX", "/music/api")
    with Session(h.engine) as session:
        user = session.get(User, h.id("user"))
        spec = _spec(dp.KIND_PLAYLIST, "cover", 3)
        spec.cover_url = "/api/cover/provider/catalog/abc"
        dr.request_download(session, user, spec, favorite_for=user.id, config=h.config, registry=h.registry,
                            auth_manager=FakeAuthManager())

    assert _only(requests_of(h.engine)).cover_url == "hostref:/cover/provider/catalog/abc"
    item = h.call("user", "GET", "/download-requests").json()["items"][0]
    assert item["cover_url"] == "/music/api/cover/provider/catalog/abc"
