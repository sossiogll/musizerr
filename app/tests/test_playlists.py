"""
The playlists of a user, through the real routes: own playlists (create,
rename, add, remove, reorder, the cap), the privacy of every playlist, the
followed ones (follow = keep + ask to download, never a favorite; read-only;
duplicate as mine), the snapshot-driven refresh and the deletion with or
without the downloaded files.

Real routes through TestClient, the database of the suite, fake providers only.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import download_states as ds
from core.download_policy import KIND_PLAYLIST, MODE_APPROVAL, mode_key
from musizeer_sdk.models import TracksUnavailable
from db.models import (
    SYSTEM_USER_ID,
    Download,
    DownloadRequest,
    PendingLike,
    Playlist,
    PlaylistTrack,
    TrackRecord,
    UserFavorite,
)
from db import playlist_store

from tests.conftest import make_track
from tests.test_openapi_responses import assert_conforms
from tests.playlists_support import build_api, make_user


@pytest.fixture
def api(engine, monkeypatch):
    return build_api(engine, monkeypatch)


@pytest.fixture
def alice(engine):
    return make_user(engine, "alice")


@pytest.fixture
def bob(engine):
    return make_user(engine, "bob")


def _create(api, user, name="Road trip") -> dict:
    response = api.call(user, "POST", "/playlists", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _track_ids(api, user, playlist_id) -> list[str]:
    detail = api.call(user, "GET", f"/playlists/{playlist_id}").json()
    return [track["library_id"] for track in detail["tracks"]]


def _titles(api, user, playlist_id) -> list[str]:
    return [t["title"] for t in api.call(user, "GET", f"/playlists/{playlist_id}").json()["tracks"]]


def _add(api, user, playlist_id, *provider_ids) -> dict:
    items = [{"provider": "catalog", "provider_id": pid} for pid in provider_ids]
    response = api.call(user, "POST", f"/playlists/{playlist_id}/tracks", json={"items": items})
    assert response.status_code == 200, response.text
    return response.json()


def _follow(api, user, **kwargs):
    return api.call(user, "POST", "/playlists/follow", json={"provider": "catalog", "id": "p1"}, **kwargs)


# ---------------------------------------------------------------------------
# Own playlists
# ---------------------------------------------------------------------------


def test_create_list_rename_and_delete_an_own_playlist(api, alice):
    created = _create(api, alice)
    assert (created["kind"], created["track_count"], created["synced_to"]) == ("own", 0, [])

    listing = api.call(alice, "GET", "/playlists").json()
    assert [item["name"] for item in listing["items"]] == ["Road trip"]
    assert listing["max_tracks"] == 500

    renamed = api.call(alice, "PATCH", f"/playlists/{created['id']}", json={"name": "Night drive"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Night drive"

    deleted = api.call(alice, "DELETE", f"/playlists/{created['id']}")
    assert deleted.json() == {"deleted": True, "tracks_scheduled": 0}
    assert api.call(alice, "GET", "/playlists").json()["items"] == []


def test_a_blank_name_and_an_empty_patch_are_refused(api, alice):
    assert api.call(alice, "POST", "/playlists", json={"name": "   "}).status_code == 422
    created = _create(api, alice)
    assert api.call(alice, "PATCH", f"/playlists/{created['id']}", json={}).status_code == 422


def test_tracks_are_added_by_provider_and_by_library_id_in_order_without_duplicates(api, alice):
    playlist = _create(api, alice)

    first = _add(api, alice, playlist["id"], "t2", "t1")
    assert (first["added"], first["skipped_duplicates"], first["playlist"]["track_count"]) == (2, 0, 2)

    by_id = _track_ids(api, alice, playlist["id"])
    again = api.call(alice, "POST", f"/playlists/{playlist['id']}/tracks", json={"items": [
        {"track_id": by_id[0]}, {"provider": "catalog", "provider_id": "t3"}, {"provider": "catalog", "provider_id": "t1"},
    ]})
    assert again.json()["added"] == 1 and again.json()["skipped_duplicates"] == 2
    assert _titles(api, alice, playlist["id"]) == ["Song 2", "Song 1", "Song 3"]


def test_an_item_must_name_the_track_one_way_only(api, alice):
    playlist = _create(api, alice)
    for item in ({}, {"track_id": "x", "provider": "catalog", "provider_id": "t1"}, {"provider": "catalog"}):
        response = api.call(alice, "POST", f"/playlists/{playlist['id']}/tracks", json={"items": [item]})
        assert response.status_code == 422


def test_an_unknown_library_track_is_a_404_and_nothing_is_added(api, alice):
    playlist = _create(api, alice)
    response = api.call(alice, "POST", f"/playlists/{playlist['id']}/tracks", json={"items": [
        {"provider": "catalog", "provider_id": "t1"}, {"track_id": "nope"},
    ]})
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "track_not_found"
    assert api.call(alice, "GET", f"/playlists/{playlist['id']}").json()["track_count"] == 0


def test_the_cap_refuses_the_whole_batch_and_counts_what_is_there(api, alice):
    api.config.update({"playlist_max_tracks": 3})
    playlist = _create(api, alice)
    _add(api, alice, playlist["id"], "t1", "t2")

    over = api.call(alice, "POST", f"/playlists/{playlist['id']}/tracks", json={"items": [
        {"provider": "catalog", "provider_id": "t3"}, {"provider": "catalog", "provider_id": "t4"},
    ]})

    assert over.status_code == 409
    assert over.json()["detail"] == {
        "error_code": "playlist_full", "error_message": "A playlist holds at most 3 tracks.",
        "max_tracks": 3, "track_count": 2, "adding": 2,
    }
    assert _titles(api, alice, playlist["id"]) == ["Song 1", "Song 2"]
    # A batch that fits is fine, up to the cap exactly.
    assert _add(api, alice, playlist["id"], "t3")["playlist"]["track_count"] == 3


def test_removing_closes_the_gap_and_is_idempotent(api, engine, alice):
    playlist = _create(api, alice)
    _add(api, alice, playlist["id"], "t1", "t2", "t3")
    middle = _track_ids(api, alice, playlist["id"])[1]

    assert api.call(alice, "DELETE", f"/playlists/{playlist['id']}/tracks/{middle}").json() == {
        "track_id": middle, "removed": True}
    assert api.call(alice, "DELETE", f"/playlists/{playlist['id']}/tracks/{middle}").json()["removed"] is False

    assert _titles(api, alice, playlist["id"]) == ["Song 1", "Song 3"]
    assert [row.position for row in _rows(engine, playlist["id"])] == [0, 1]


def test_reorder_takes_exactly_the_tracks_of_the_playlist(api, alice):
    playlist = _create(api, alice)
    _add(api, alice, playlist["id"], "t1", "t2", "t3")
    a, b, c = _track_ids(api, alice, playlist["id"])

    ok = api.call(alice, "PUT", f"/playlists/{playlist['id']}/tracks/order", json={"track_ids": [c, a, b]})
    assert ok.status_code == 200
    assert _titles(api, alice, playlist["id"]) == ["Song 3", "Song 1", "Song 2"]

    for bad in ([a, b], [a, b, c, c], [a, b, "other"], []):
        refused = api.call(alice, "PUT", f"/playlists/{playlist['id']}/tracks/order", json={"track_ids": bad})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error_code"] == "playlist_invalid_order"
    assert _titles(api, alice, playlist["id"]) == ["Song 3", "Song 1", "Song 2"]


# ---------------------------------------------------------------------------
# Privacy and sign-in
# ---------------------------------------------------------------------------


def test_a_playlist_is_private_to_its_owner(api, alice, bob):
    playlist = _create(api, alice)
    _add(api, alice, playlist["id"], "t1")
    pid = playlist["id"]

    assert api.call(bob, "GET", "/playlists").json()["items"] == []
    requests = [
        ("GET", f"/playlists/{pid}", None),
        ("PATCH", f"/playlists/{pid}", {"name": "x"}),
        ("DELETE", f"/playlists/{pid}", None),
        ("POST", f"/playlists/{pid}/tracks", {"items": [{"provider": "catalog", "provider_id": "t2"}]}),
        ("PUT", f"/playlists/{pid}/tracks/order", {"track_ids": []}),
        ("DELETE", f"/playlists/{pid}/tracks/anything", None),
        ("POST", f"/playlists/{pid}/duplicate", None),
        ("POST", f"/playlists/{pid}/refresh", None),
    ]
    for method, url, body in requests:
        response = api.call(bob, method, url, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, url)
        assert response.json()["detail"]["error_code"] == "playlist_not_found"
    assert _titles(api, alice, pid) == ["Song 1"]


def test_a_guest_cannot_touch_playlists(api):
    for method, url in (("GET", "/playlists"), ("POST", "/playlists"), ("GET", "/playlists/x"),
                        ("POST", "/playlists/follow")):
        assert api.call(None, method, url, json={}).status_code == 401


def test_with_authentication_off_the_playlists_belong_to_the_instance(engine, monkeypatch):
    api = build_api(engine, monkeypatch, auth=False)
    created = api.call(None, "POST", "/playlists", json={"name": "Everyone's"})
    assert created.status_code == 201
    assert api.call(None, "GET", "/playlists").json()["items"][0]["name"] == "Everyone's"
    with Session(engine) as session:
        assert [p.user_id for p in session.exec(select(Playlist)).all()] == [SYSTEM_USER_ID]


# ---------------------------------------------------------------------------
# Following
# ---------------------------------------------------------------------------


def test_following_keeps_the_tracks_and_asks_for_their_download_but_makes_no_favorite(api, engine, alice):
    response = _follow(api, alice)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["download"] == {"status": "queued", "queued": 3, "skipped": 0}
    playlist = body["playlist"]
    assert (playlist["kind"], playlist["name"], playlist["track_count"], playlist["source_provider"],
            playlist["source_id"], playlist["auto_download"]) == ("followed", "Mix", 3, "catalog", "p1", True)
    assert len(api.queued) == 3
    with Session(engine) as session:
        # The tracks are downloaded for the playlist: never a favorite, never a heart waiting to be applied.
        assert session.exec(select(UserFavorite)).all() == []
        assert session.exec(select(PendingLike)).all() == []
        assert len(session.exec(select(Download)).all()) == 3
        assert [row.download_requested for row in playlist_store.track_rows(session, playlist["id"])] == [True] * 3


def test_following_twice_is_one_follow_and_asks_for_nothing_more(api, alice):
    first = _follow(api, alice)
    second = _follow(api, alice)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["created"] is False and second.json()["download"] == {"status": "none"}
    assert second.json()["playlist"]["id"] == first.json()["playlist"]["id"]
    assert len(api.queued) == 3
    assert len(api.call(alice, "GET", "/playlists").json()["items"]) == 1


def test_a_followed_playlist_is_read_only_and_can_be_duplicated_as_mine(api, alice):
    followed = _follow(api, alice).json()["playlist"]
    pid = followed["id"]

    assert api.call(alice, "PATCH", f"/playlists/{pid}", json={"name": "Mine now"}).status_code == 409
    for method, url, body in (
        ("POST", f"/playlists/{pid}/tracks", {"items": [{"provider": "catalog", "provider_id": "t9"}]}),
        ("PUT", f"/playlists/{pid}/tracks/order", {"track_ids": []}),
        ("DELETE", f"/playlists/{pid}/tracks/anything", None),
    ):
        refused = api.call(alice, method, url, **({"json": body} if body is not None else {}))
        assert refused.status_code == 409
        assert refused.json()["detail"]["error_code"] == "playlist_read_only"

    copy = api.call(alice, "POST", f"/playlists/{pid}/duplicate", json={"name": "Mix, mine"})
    assert copy.status_code == 201
    assert (copy.json()["kind"], copy.json()["name"], copy.json()["track_count"], copy.json()["source_provider"]) == (
        "own", "Mix, mine", 3, None)
    assert _titles(api, alice, copy.json()["id"]) == ["Song 1", "Song 2", "Song 3"]
    # The copy is the user's to edit; the followed one is untouched.
    extra = _add(api, alice, copy.json()["id"], "t9")
    assert extra["playlist"]["track_count"] == 4
    assert api.call(alice, "GET", f"/playlists/{pid}").json()["track_count"] == 3
    assert api.call(alice, "POST", f"/playlists/{pid}/duplicate").json()["name"] == "Mix (copy)"


def test_a_followed_playlist_only_changes_whether_it_downloads(api, alice):
    pid = _follow(api, alice).json()["playlist"]["id"]
    assert api.call(alice, "PATCH", f"/playlists/{pid}", json={"auto_download": False}).json()["auto_download"] is False
    own = _create(api, alice)
    refused = api.call(alice, "PATCH", f"/playlists/{own['id']}", json={"auto_download": True})
    assert refused.status_code == 422 and refused.json()["detail"]["error_code"] == "playlist_not_followed"


def test_a_playlist_that_waits_for_approval_is_followed_all_the_same(api, engine, alice):
    api.config.update({mode_key(KIND_PLAYLIST): MODE_APPROVAL})

    response = _follow(api, alice)

    assert response.status_code == 201
    download = response.json()["download"]
    assert (download["status"], download["needed_tracks"], download["reason"]) == (
        "pending_approval", 3, "kind_requires_approval")
    assert api.queued == []
    assert response.json()["playlist"]["track_count"] == 3
    with Session(engine) as session:
        requests = session.exec(select(DownloadRequest)).all()
        assert [(r.kind, r.status, r.add_to_favorites) for r in requests] == [("playlist", "pending", False)]
        assert session.exec(select(Download)).all() == []


def test_a_provider_that_hides_the_tracks_is_followed_with_the_reason(api, alice):
    api.source.unavailable = TracksUnavailable("access_restricted", "Not shared with this app.")

    response = _follow(api, alice)

    assert response.status_code == 201
    playlist = response.json()["playlist"]
    assert playlist["tracks_unavailable"] == {"code": "access_restricted", "message": "Not shared with this app."}
    assert playlist["track_count"] == 0
    assert response.json()["download"] == {"status": "none"}
    assert api.queued == []


def test_a_playlist_longer_than_the_cap_is_not_followed(api, alice):
    api.config.update({"playlist_max_tracks": 2})

    response = _follow(api, alice)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "playlist_full"
    assert (response.json()["detail"]["max_tracks"], response.json()["detail"]["adding"]) == (2, 3)
    assert api.call(alice, "GET", "/playlists").json()["items"] == []
    assert api.queued == []


def test_following_a_playlist_that_is_not_there_is_a_404(api, alice):
    api.source.exists = False
    assert _follow(api, alice).status_code == 404


def test_tracks_already_in_the_library_are_not_asked_for_again(api, engine, alice):
    _follow(api, alice)
    with Session(engine) as session:
        for download in session.exec(select(Download)).all():
            download.status = ds.DOWNLOADED
            session.add(download)
        session.commit()
    api.queued.clear()
    other = make_user(engine, "carol")

    response = _follow(api, other)

    assert response.status_code == 201
    assert response.json()["download"] == {"status": "none", "queued": 0, "skipped": 3}
    assert api.queued == []
    with Session(engine) as session:
        flagged = playlist_store.track_rows(session, response.json()["playlist"]["id"])
        assert [row.download_requested for row in flagged] == [False, False, False]


# ---------------------------------------------------------------------------
# Watching the source
# ---------------------------------------------------------------------------


def test_refresh_reads_the_tracks_again_and_follows_the_source(api, engine, alice):
    pid = _follow(api, alice).json()["playlist"]["id"]
    api.queued.clear()
    api.source.tracks = [api.source.tracks[2], api.source.tracks[0]] + [
        make_track(title="Song 4", provider="catalog", provider_id="t4")]
    api.source.snapshot = "v2"

    result = api.call(alice, "POST", f"/playlists/{pid}/refresh").json()

    assert (result["changed"], result["new_tracks"], result["removed_tracks"], result["error_code"]) == (True, 1, 1, None)
    assert result["download"] == {"status": "queued", "queued": 1, "skipped": 0}
    assert len(api.queued) == 1
    assert _titles(api, alice, pid) == ["Song 3", "Song 1", "Song 4"]
    with Session(engine) as session:
        assert playlist_store.get(session, pid).snapshot_id == "v2"
        # The track the source dropped leaves the playlist; its record and files stay.
        assert session.exec(select(TrackRecord).where(TrackRecord.title == "Song 2")).first() is not None


def test_refresh_does_not_download_when_the_playlist_is_not_asked_to(api, alice):
    pid = _follow(api, alice).json()["playlist"]["id"]
    api.call(alice, "PATCH", f"/playlists/{pid}", json={"auto_download": False})
    api.queued.clear()
    api.source.tracks = api.source.tracks + [
        make_track(title="Song 4", provider="catalog", provider_id="t4")]
    api.source.snapshot = "v2"

    result = api.call(alice, "POST", f"/playlists/{pid}/refresh").json()

    assert (result["new_tracks"], result["download"]) == (1, {"status": "none"})
    assert api.queued == []


def test_refresh_of_an_own_playlist_is_refused(api, alice):
    own = _create(api, alice)
    response = api.call(alice, "POST", f"/playlists/{own['id']}/refresh")
    assert response.status_code == 422 and response.json()["detail"]["error_code"] == "playlist_not_followed"


def test_a_source_that_went_away_is_recorded_and_the_tracks_stay(api, alice):
    pid = _follow(api, alice).json()["playlist"]["id"]
    api.source.exists = False

    result = api.call(alice, "POST", f"/playlists/{pid}/refresh").json()

    assert result["error_code"] == "playlist_source_not_found" and result["changed"] is False
    assert result["playlist"]["refresh_error_code"] == "playlist_source_not_found"
    assert result["playlist"]["track_count"] == 3


def test_a_source_that_stops_sharing_its_tracks_keeps_what_was_read(api, alice):
    pid = _follow(api, alice).json()["playlist"]["id"]
    api.source.unavailable = TracksUnavailable("access_restricted", "Not shared.")

    result = api.call(alice, "POST", f"/playlists/{pid}/refresh").json()

    assert result["changed"] is False
    assert result["playlist"]["track_count"] == 3
    assert result["playlist"]["tracks_unavailable"]["code"] == "access_restricted"


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_deleting_removes_the_playlist_and_nothing_else_by_default(api, engine, alice, sent_tasks):
    pid = _follow(api, alice).json()["playlist"]["id"]
    sent_tasks.clear()

    response = api.call(alice, "DELETE", f"/playlists/{pid}")

    assert response.json() == {"deleted": True, "tracks_scheduled": 0}
    assert [name for name, _, _ in sent_tasks if name == "workers.maintenance.delete_playlist_tracks"] == []
    with Session(engine) as session:
        assert session.exec(select(PlaylistTrack)).all() == []
        assert len(session.exec(select(TrackRecord)).all()) == 3
        assert len(session.exec(select(Download)).all()) == 3


def test_deleting_with_the_files_schedules_only_what_nothing_else_holds(api, engine, alice, bob, sent_tasks):
    pid = _follow(api, alice).json()["playlist"]["id"]
    held_by_playlist, held_by_favorite, free = [
        row.track_id for row in _rows(engine, pid)]
    # One track is also in another playlist, one is somebody's favorite.
    other = _create(api, bob, "Bob's")
    api.call(bob, "POST", f"/playlists/{other['id']}/tracks", json={"items": [{"track_id": held_by_playlist}]})
    with Session(engine) as session:
        session.add(UserFavorite(user_id=bob.id, track_id=held_by_favorite))
        session.commit()
    sent_tasks.clear()

    response = api.call(alice, "DELETE", f"/playlists/{pid}", params={"delete_tracks": "true"})

    assert response.json() == {"deleted": True, "tracks_scheduled": 1}
    assert [(name, kwargs) for name, kwargs, _ in sent_tasks if name == "workers.maintenance.delete_playlist_tracks"] == [
        ("workers.maintenance.delete_playlist_tracks", {"user_id": alice.id, "track_ids": [free]})]


def test_an_own_playlist_never_schedules_a_deletion(api, alice, sent_tasks):
    playlist = _create(api, alice)
    _add(api, alice, playlist["id"], "t1")
    sent_tasks.clear()

    response = api.call(alice, "DELETE", f"/playlists/{playlist['id']}", params={"delete_tracks": "true"})

    assert response.json() == {"deleted": True, "tracks_scheduled": 0}


def _rows(engine, playlist_id):
    with Session(engine) as session:
        return [row for row in playlist_store.track_rows(session, playlist_id)]


# ---------------------------------------------------------------------------
# The documented answers are the real ones
# ---------------------------------------------------------------------------


def test_every_answer_conforms_to_the_documented_schema(api, alice, bob):
    def check(response, method, path):
        assert_conforms(response, method, path)
        return response

    created = check(api.call(alice, "POST", "/playlists", json={"name": "Road", "description": "d"}), "POST", "/playlists")
    pid = created.json()["id"]
    check(api.call(alice, "GET", "/playlists"), "GET", "/playlists")
    check(api.call(alice, "PATCH", f"/playlists/{pid}", json={"name": "Road 2"}), "PATCH", "/playlists/{playlist_id}")
    added = check(api.call(alice, "POST", f"/playlists/{pid}/tracks", json={"items": [
        {"provider": "catalog", "provider_id": "t1"}, {"provider": "catalog", "provider_id": "t2"}]}),
        "POST", "/playlists/{playlist_id}/tracks")
    assert added.json()["added"] == 2
    detail = check(api.call(alice, "GET", f"/playlists/{pid}"), "GET", "/playlists/{playlist_id}")
    first, second = [t["library_id"] for t in detail.json()["tracks"]]
    check(api.call(alice, "PUT", f"/playlists/{pid}/tracks/order", json={"track_ids": [second, first]}),
          "PUT", "/playlists/{playlist_id}/tracks/order")
    check(api.call(alice, "PUT", f"/playlists/{pid}/tracks/order", json={"track_ids": [first]}),
          "PUT", "/playlists/{playlist_id}/tracks/order")
    check(api.call(alice, "DELETE", f"/playlists/{pid}/tracks/{first}"), "DELETE",
          "/playlists/{playlist_id}/tracks/{track_id}")
    check(api.call(alice, "POST", f"/playlists/{pid}/duplicate", json={"name": "Copy"}), "POST",
          "/playlists/{playlist_id}/duplicate")
    api.config.update({"playlist_max_tracks": 1})
    check(api.call(alice, "POST", f"/playlists/{pid}/tracks", json={"items": [
        {"provider": "catalog", "provider_id": "t3"}]}), "POST", "/playlists/{playlist_id}/tracks")
    api.config.update({"playlist_max_tracks": 500})
    followed = check(_follow(api, alice), "POST", "/playlists/follow")
    check(_follow(api, alice), "POST", "/playlists/follow")
    fid = followed.json()["playlist"]["id"]
    check(api.call(alice, "GET", f"/playlists/{fid}"), "GET", "/playlists/{playlist_id}")
    check(api.call(alice, "PATCH", f"/playlists/{fid}", json={"name": "no"}), "PATCH", "/playlists/{playlist_id}")
    check(api.call(alice, "POST", f"/playlists/{fid}/refresh"), "POST", "/playlists/{playlist_id}/refresh")
    check(api.call(alice, "POST", f"/playlists/{pid}/refresh"), "POST", "/playlists/{playlist_id}/refresh")
    check(api.call(bob, "GET", f"/playlists/{pid}"), "GET", "/playlists/{playlist_id}")
    check(api.call(alice, "DELETE", f"/playlists/{fid}", params={"delete_tracks": "true"}), "DELETE",
          "/playlists/{playlist_id}")
    check(api.call(alice, "DELETE", f"/playlists/{pid}"), "DELETE", "/playlists/{playlist_id}")
    check(api.call(None, "GET", "/playlists"), "GET", "/playlists")
