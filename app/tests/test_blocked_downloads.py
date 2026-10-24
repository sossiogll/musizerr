"""
While a folder is not set the product is BLOCKED, not guessing (ADR-0008): the one download gate refuses with
``working_folder_not_set`` or ``library_folder_not_set`` for every entry point (track, album, playlist, artist, the likes
download, a retry, an approval), nothing is queued and no approval request is recorded; a task queued before the setting
was cleared ends failed and retriable; the other entries that need a workspace refuse the same way. The real
``ConfigManager.workspace_dir`` is used (the suite's autouse fixture replaces it with a temporary folder).
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from core import download_policy as dp
from db.models import Download, TrackIdentifierRecord, TrackRecord, UserSavedTrack
from db.track_store import find_or_create_track
from services import downloads, folders
from services.config_manager import ConfigManager
from services.folders import FoldersNotReady
from tests.conftest import FakeAuthManager, FakeRegistry, FakeSyncProvider
from tests.download_requests_support import Catalog, Harness, downloads_of, intents_of, requests_of, tracks_of
from tests.folders_support import FolderStorage, RemoteStorage, UndeclaredFolderStorage, add_root, store_folder, store_provider_folder
from services import download_requests as dr

REAL_WORKING_FOLDER = ConfigManager.__dict__["workspace_dir"]  # captured before any test replaces it

ROUTES = [
    ("track", "/download/track", {"provider": "catalog", "id": "t-1"}),
    ("album", "/download/album", {"provider": "catalog", "id": "album-x-3"}),
    ("playlist", "/download/playlist", {"provider": "catalog", "id": "playlist-x-3"}),
    ("artist", "/download/artist", {"provider": "catalog", "id": "artist-x-2"}),
]


@pytest.fixture
def h(engine, monkeypatch):
    monkeypatch.setattr(ConfigManager, "workspace_dir", REAL_WORKING_FOLDER)
    harness = Harness(engine, monkeypatch)
    harness.registry = FakeRegistry(metadata=[Catalog()], storage=[FolderStorage(), UndeclaredFolderStorage(), RemoteStorage()])
    return harness


def set_working_folder(h: Harness, path: str = "/downloads", relative: str = ".musizeer-work", **root_options):
    root = add_root(h.engine, path, **root_options)
    store_folder(h.engine, "workspace_dir", root, relative)
    h.config.committed({"workspace_dir": {"root_id": root.id, "path": relative}})
    return root


def use_storage(h: Harness, name: str) -> None:
    h.config.update({"storage_provider": name})


def nothing_happened(h: Harness) -> None:
    assert downloads_of(h.engine) == [] and h.queued == [] and requests_of(h.engine) == [] and intents_of(h.engine) == []


# ------------------------------------------------------------------
# The working folder
# ------------------------------------------------------------------


@pytest.mark.parametrize("who", ["user", "admin", "open"])
@pytest.mark.parametrize("name, url, body", ROUTES, ids=[r[0] for r in ROUTES])
def test_with_no_working_folder_every_download_route_answers_409_and_nothing_is_queued(engine, monkeypatch, who, name, url, body):
    monkeypatch.setattr(ConfigManager, "workspace_dir", REAL_WORKING_FOLDER)
    h = Harness(engine, monkeypatch, auth=who != "open")

    reply = h.call(who, "POST", url, json=body, doc=url)

    assert reply.status_code == 409
    assert reply.json()["detail"]["error_code"] == "working_folder_not_set"
    assert "working folder" in reply.json()["detail"]["error_message"]
    nothing_happened(h)


@pytest.mark.parametrize("name, url, body", ROUTES, ids=[r[0] for r in ROUTES])
def test_a_request_the_policy_would_hold_for_approval_is_not_even_recorded(h, name, url, body):
    h.policy(all_modes=dp.MODE_APPROVAL)

    reply = h.call("user", "POST", url, json=body)

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "working_folder_not_set"
    nothing_happened(h)


def test_the_error_detail_says_why_but_only_to_an_administrator(h):
    root = add_root(h.engine, "/etc", state="refused", reason="not a mounted volume")
    store_folder(h.engine, "workspace_dir", root, "work")
    h.config.committed({"workspace_dir": {"root_id": root.id, "path": "work"}})

    mine = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}).json()["detail"]
    admin = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}).json()["detail"]

    assert mine["error_code"] == admin["error_code"] == "working_folder_not_set" and "error_detail" not in mine
    assert admin["error_detail"] == "not a mounted volume"


def test_a_working_folder_in_a_root_the_gateway_refuses_counts_as_not_set(h):
    set_working_folder(h, "/nowhere", state="refused", reason="not writable")

    reply = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 409 and reply.json()["detail"]["error_detail"] == "not writable"
    nothing_happened(h)


def test_with_the_working_folder_set_the_same_request_goes_through(h):
    set_working_folder(h)

    reply = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}, doc="/download/track")

    assert reply.status_code == 200 and len(h.queued) == 1


def test_an_old_text_stored_as_the_working_folder_is_not_a_folder(h):
    h.config.committed({"workspace_dir": "/downloads/.musizeer-work"})

    reply = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "working_folder_not_set"


def test_the_real_workspace_dir_resolves_the_working_folder_and_raises_while_it_is_not_set(h):
    with pytest.raises(FoldersNotReady) as blocked:
        h.config.workspace_dir

    assert blocked.value.error_code == "working_folder_not_set"
    set_working_folder(h, "/downloads", "a/b")
    assert str(h.config.workspace_dir) == "/downloads/a/b"


# ------------------------------------------------------------------
# The library folder
# ------------------------------------------------------------------


def test_with_a_storage_whose_library_folder_is_not_set_downloads_are_blocked_with_its_own_code(h):
    set_working_folder(h)
    use_storage(h, "foldered")

    reply = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}, doc="/download/track")

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "library_folder_not_set"
    nothing_happened(h)


def test_the_library_folder_is_judged_after_the_working_folder(h):
    use_storage(h, "foldered")

    reply = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.json()["detail"]["error_code"] == "working_folder_not_set"


def test_once_the_library_folder_is_set_downloads_go(h):
    set_working_folder(h)
    library = add_root(h.engine, "/library")
    store_provider_folder(h.engine, "foldered", "root_dir", library, "Music")
    use_storage(h, "foldered")

    reply = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 200 and len(h.queued) == 1


def test_a_library_folder_in_a_refused_root_counts_as_not_set(h):
    set_working_folder(h)
    library = add_root(h.engine, "/library", state="refused", reason="not a mounted volume")
    store_provider_folder(h.engine, "foldered", "root_dir", library, "")
    use_storage(h, "foldered")

    reply = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "library_folder_not_set"
    assert reply.json()["detail"]["error_detail"] == "not a mounted volume; set the library folder of Foldered in Root folders"


def test_the_blocked_state_of_an_unset_library_folder_says_where_to_set_it(h):
    set_working_folder(h)
    use_storage(h, "foldered")

    detail = h.call("admin", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}).json()["detail"]
    status = status_of(h, "admin")["library_folder"]

    assert detail["error_detail"] == "set the library folder of Foldered in Root folders"
    assert status["detail"] == "set the library folder of Foldered in Root folders" and status["setting"] == "provider.foldered.root_dir"
    assert "detail" not in status_of(h, "user")["library_folder"], "the words are for administrators"


def test_a_storage_without_a_declared_library_uses_its_only_folder_setting(h):
    set_working_folder(h)
    use_storage(h, "undeclared")

    reply = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "library_folder_not_set"


def test_a_storage_whose_library_is_not_a_folder_needs_no_library_folder(h):
    set_working_folder(h)
    use_storage(h, "remote")

    assert h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}).status_code == 200


def test_with_no_storage_provider_active_only_the_working_folder_is_needed(h):
    set_working_folder(h)

    assert h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"}).status_code == 200


# ------------------------------------------------------------------
# The other ways in
# ------------------------------------------------------------------


def test_the_service_entry_points_refuse_before_recording_anything(h):
    from db.models import User

    with Session(h.engine) as session:
        user = session.get(User, h.id("user"))
        with pytest.raises(FoldersNotReady):
            dr.request_download(
                session, user, dr.track_spec(tracks_of("svc", 1)[0]), favorite_for=user.id, config=h.config,
                registry=h.registry, auth_manager=FakeAuthManager(),
            )
        with pytest.raises(FoldersNotReady):
            downloads.enqueue_track(tracks_of("svc2", 1)[0], session, user_id=user.id, favorite_for=user.id, registry=h.registry)
    nothing_happened(h)


def test_a_retry_is_blocked_too_and_leaves_the_failed_download_as_it_was(h):
    with Session(h.engine) as session:
        record = TrackRecord(title="t", artist="a", album="b")
        session.add(record)
        session.flush()
        session.add(Download(track_id=record.internal_id, status="failed", retriable=True))
        session.commit()
        track_id = record.internal_id

    reply = h.call("admin", "POST", f"/downloads/{track_id}/retry", doc="/downloads/{track_id}/retry")

    assert reply.status_code == 409 and reply.json()["detail"]["error_code"] == "working_folder_not_set"
    assert [d.status for d in downloads_of(h.engine)] == ["failed"] and h.queued == []


def test_the_routine_of_the_transient_failures_waits_and_says_why(h):
    with Session(h.engine) as session:
        result = downloads.retry_transient_failures(session, h.registry, h.config)

    assert result == {"candidates": 0, "queued": 0, "blocked": "working_folder_not_set"}


def test_an_approval_waits_while_a_folder_is_missing_and_goes_once_it_is_set(h):
    set_working_folder(h)
    h.policy(all_modes=dp.MODE_APPROVAL)
    pending = h.call("user", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})
    assert pending.status_code == 202
    request_id = pending.json()["request_id"]
    h.config.committed({"workspace_dir": None})

    result = h.call("admin", "POST", "/admin/download-requests/approve", json={"ids": [request_id]}).json()["results"][0]

    assert result["outcome"] == "failed", "refused before it was decided: nothing was queued"
    assert [r.status for r in requests_of(h.engine)] == ["pending"] and h.queued == []

    h.config.committed({"workspace_dir": {"root_id": requests_root(h), "path": ".musizeer-work"}})
    approved = h.call("admin", "POST", "/admin/download-requests/approve", json={"ids": [request_id]}).json()["results"][0]
    assert approved["outcome"] == "decided" and len(h.queued) == 1


def requests_root(h) -> str:
    from db import root_store

    with Session(h.engine) as session:
        return root_store.list_roots(session)[0].id


def _likes(engine, user_id: str, count: int) -> None:
    with Session(engine) as session:
        for i in range(count):
            record = TrackRecord(
                title=f"liked {i}", artist="A", album="B",
                track_data=json.dumps({"title": f"liked {i}", "artist": "A", "album": "B", "duration_ms": 1}),
            )
            session.add(record)
            session.flush()
            session.add(TrackIdentifierRecord(track_id=record.internal_id, id_type="isrc", value=f"ISRC-liked-{i}"))
            session.add(UserSavedTrack(user_id=user_id, track_id=record.internal_id))
        session.commit()


def _run_likes_job(h: Harness, who: str, monkeypatch):
    from db import library_job_store as job_store
    from workers import tasks

    monkeypatch.setattr(tasks, "engine", h.engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: h.registry)
    monkeypatch.setattr(tasks, "get_config", lambda: h.config)
    with Session(h.engine) as session:
        run = job_store.create_run(session, h.id(who), "download_sync", params={"time_range": "all"})
    result = tasks.library_download_sync.run(user_id=h.id(who), time_range="all", run_id=run.id)
    with Session(h.engine) as session:
        return result, session.get(type(run), run.id)


@pytest.mark.parametrize("who", ["user", "admin"])
def test_the_likes_download_ends_failed_with_the_code_and_queues_nothing(h, monkeypatch, who):
    h.policy(liked_elsewhere_mode=dp.MODE_APPROVAL)
    _likes(h.engine, h.id(who), 3)

    result, run = _run_likes_job(h, who, monkeypatch)

    assert result["status"] == "error" and result["error_code"] == "working_folder_not_set"
    assert run.status == "failed" and run.error_code == "working_folder_not_set"
    nothing_happened(h)


def test_the_likes_download_names_the_library_folder_when_that_is_the_missing_one(h, monkeypatch):
    set_working_folder(h)
    use_storage(h, "foldered")
    _likes(h.engine, h.id("admin"), 2)

    result, run = _run_likes_job(h, "admin", monkeypatch)

    assert run.status == "failed" and run.error_code == "library_folder_not_set" and result["error_code"] == "library_folder_not_set"
    nothing_happened(h)


# ------------------------------------------------------------------
# A task queued before the setting was cleared
# ------------------------------------------------------------------


def test_a_queued_download_checks_again_before_it_downloads_and_ends_failed_and_retriable(h, monkeypatch):
    from workers import tasks

    track = tracks_of("late", 1)[0]
    with Session(h.engine) as session:
        record = find_or_create_track(track, session)
        download = Download(track_id=record.internal_id, status="pending")
        session.add(download)
        session.commit()
        download_id = download.id
        task_data = downloads.task_track_data(session, record)
    monkeypatch.setattr(tasks, "engine", h.engine)
    monkeypatch.setattr(tasks, "get_registry", lambda: h.registry)
    monkeypatch.setattr(tasks, "get_config", lambda: h.config)
    monkeypatch.setattr(tasks, "publish_download_progress", lambda *args, **kwargs: None)

    result = tasks.download_task.run(track_data=task_data, download_id=download_id)

    assert result == {"status": "failed", "error": "working_folder_not_set"}
    (row,) = downloads_of(h.engine)
    assert row.status == "failed" and row.retriable is True and row.error_message.startswith("working_folder_not_set:")
    assert row.completed_at is not None


# ------------------------------------------------------------------
# Every other entry that needs a workspace
# ------------------------------------------------------------------


def test_the_full_sync_probe_is_refused_with_the_code(h, monkeypatch):
    from workers import maintenance

    h.config.update({"storage_provider": "foldered", "sync_provider": "fakesync"})
    h.registry = FakeRegistry(metadata=[Catalog()], storage=[FolderStorage()], syncs=[FakeSyncProvider("fakesync")])
    monkeypatch.setattr(maintenance, "get_config", lambda: h.config)
    monkeypatch.setattr(maintenance, "get_registry", lambda: h.registry)
    monkeypatch.setattr(maintenance, "engine", h.engine)

    result = maintenance.verify_full_sync.run()

    assert result["status"] == "error" and result["error_code"] == "working_folder_not_set"


def test_the_sweep_has_nothing_to_sweep_without_a_working_folder(h, monkeypatch):
    from workers import maintenance

    monkeypatch.setattr(maintenance, "get_config", lambda: h.config)

    assert maintenance.sweep_workspaces.run() == {"status": "ok", "removed": 0, "held_removed": 0, "skipped": "working_folder_not_set"}


def test_the_library_scan_waits_and_says_why(h):
    from services import library_scan

    use_storage(h, "foldered")
    with Session(h.engine) as session:
        result = library_scan.import_existing_library(session, h.registry, h.config, FakeAuthManager())

    assert result["state"] == "waiting" and result["error_code"] == "working_folder_not_set"
    with Session(h.engine) as session:
        assert library_scan.scan_library_once(session, h.registry, h.config, FakeAuthManager(), force=True) is None


# ------------------------------------------------------------------
# What the viewer status carries
# ------------------------------------------------------------------


def status_of(h: Harness, who: str) -> dict:
    reply = h.call(who, "GET", "/auth/account/status", doc="/auth/account/status")
    assert reply.status_code == 200
    return reply.json()["folders"]


def test_the_status_of_a_new_install_says_both_the_working_folder_and_nothing_else_is_missing(h):
    status = status_of(h, "user")

    assert status["ready"] is False and status["missing"] == ["working_folder"]
    assert status["working_folder"] == {"set": False, "state": "not_set", "applicable": True}
    assert status["library_folder"] == {"set": True, "state": "set", "applicable": False}


def test_an_administrator_also_sees_the_setting_and_the_reason(h):
    root = add_root(h.engine, "/etc", state="refused", reason="not a mounted volume")
    store_folder(h.engine, "workspace_dir", root, "work")
    h.config.committed({"workspace_dir": {"root_id": root.id, "path": "work"}})

    status = status_of(h, "admin")

    assert status["working_folder"] == {
        "set": False, "state": "root_refused", "applicable": True, "setting": "workspace_dir", "detail": "not a mounted volume",
    }


def test_both_folders_are_listed_when_both_are_missing(h):
    use_storage(h, "foldered")
    set_working_folder(h)
    h.config.committed({"workspace_dir": None})

    status = status_of(h, "admin")

    assert status["missing"] == ["working_folder", "library_folder"]
    assert status["library_folder"]["setting"] == "provider.foldered.root_dir" and status["library_folder"]["applicable"] is True


def test_the_status_is_ready_when_the_folders_are_set(h):
    set_working_folder(h)
    library = add_root(h.engine, "/library")
    store_provider_folder(h.engine, "foldered", "root_dir", library)
    use_storage(h, "foldered")

    status = status_of(h, "user")

    assert status["ready"] is True and status["missing"] == []
    assert status["library_folder"] == {"set": True, "state": "set", "applicable": True}


def test_a_guest_has_no_folder_status_and_open_mode_sees_the_details(engine, monkeypatch):
    monkeypatch.setattr(ConfigManager, "workspace_dir", REAL_WORKING_FOLDER)
    guest = Harness(engine, monkeypatch)
    assert guest.call("guest", "GET", "/auth/account/status").json()["folders"] is None

    open_mode = Harness(engine, monkeypatch, auth=False)
    status = open_mode.call("open", "GET", "/auth/account/status").json()["folders"]
    assert status["working_folder"]["setting"] == "workspace_dir"


def test_the_error_handler_words_nothing_for_a_guest_on_a_closed_instance(h):
    reply = h.call("guest", "POST", "/download/track", json={"provider": "catalog", "id": "t-1"})

    assert reply.status_code == 401
